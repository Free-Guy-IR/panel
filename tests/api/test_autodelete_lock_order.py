import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import base
from app.db.crud import user as user_crud
from app.db.models import User, UserStatus
from app.utils.crypto import generate_wireguard_keypair
from tests.api import DATABASE_URL, IS_SQLITE, app, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    unique_name,
)
from tests.api.lock_waits import a_lock_wait_appears

HOLD_SECONDS = 0.5


def _wireguard_world(access_token, prefix, expiring_payload=None):
    interface_name = unique_name("wg_autodelete")
    private_key, _ = generate_wireguard_keypair()
    core = create_core(
        access_token,
        name=unique_name("wg_autodelete_core"),
        config={
            "interface_name": interface_name,
            "private_key": private_key,
            "listen_port": 51820,
            "address": [f"{prefix}.1/29"],
        },
        type="wg",
        fallbacks=[],
    )
    group = create_group(access_token, name=unique_name("autodelete_group"), inbound_tags=[interface_name])
    expiring = create_user(
        access_token,
        group_ids=[group["id"]],
        payload={"username": unique_name("autodelete_expiring"), **(expiring_payload or {})},
    )
    other = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("autodelete_other")})
    return core, group, expiring, other


def _drop_world(access_token, core, group, users):
    for user in users:
        response = client.delete(f"/api/user/{user['username']}", headers=auth_headers(access_token))
        assert response.status_code in (status.HTTP_204_NO_CONTENT, status.HTTP_404_NOT_FOUND), response.text
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


async def _expire_for_autodelete(session_factory, user_id):
    async with session_factory() as db:
        long_ago = datetime.now(UTC) - timedelta(days=40)
        await db.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.expired, last_status_change=long_ago, auto_delete_in_days=0)
        )
        await db.commit()


async def _stored_statuses(session_factory, user_ids):
    async with session_factory() as db:
        rows = (await db.execute(select(User.id, User.status).where(User.id.in_(user_ids)))).all()
    return {row.id: row.status for row in rows}


def _race_the_autodelete_job(monkeypatch, access_token, expiring, other, send):
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {})
    session_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    real_load = user_crud.load_user_attrs
    headers = auth_headers(access_token)
    race = {"client": None, "request": None, "waiting": None}

    async def load_while_the_request_waits(user, **kwargs):
        if race["request"] is None:
            race["request"] = asyncio.create_task(send(race["client"], headers))
            observed = await a_lock_wait_appears(engine, HOLD_SECONDS)
            race["waiting"] = observed if observed is not None else not race["request"].done()
        return await real_load(user, **kwargs)

    async def racing_db():
        async with session_factory() as db:
            yield db

    async def run():
        await _expire_for_autodelete(session_factory, expiring["id"])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as racing_client:
            race["client"] = racing_client
            async with session_factory() as db:
                deleted = await user_crud.autodelete_expired_users(db)
            outcome = (await asyncio.gather(race["request"], return_exceptions=True))[0]
        stored = await _stored_statuses(session_factory, [expiring["id"], other["id"]])
        await engine.dispose()
        return [user.id for user in deleted], outcome, stored

    monkeypatch.setattr(user_crud, "load_user_attrs", load_while_the_request_waits)
    previous_get_db = app.dependency_overrides[base.get_db]
    app.dependency_overrides[base.get_db] = racing_db
    try:
        deleted, outcome, stored = asyncio.run(run())
    finally:
        app.dependency_overrides[base.get_db] = previous_get_db
        monkeypatch.setattr(user_crud, "load_user_attrs", real_load)
    return deleted, outcome, stored, race["waiting"]


def test_an_admin_bulk_delete_during_the_autodelete_job_waits_for_it_instead_of_deadlocking(access_token, monkeypatch):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")
    core, group, expiring, other = _wireguard_world(access_token, "10.49.1")
    try:

        async def delete_the_other_user(racing_client, headers):
            return await racing_client.post("/api/users/bulk/delete", headers=headers, json={"ids": [other["id"]]})

        deleted, outcome, stored, waiting = _race_the_autodelete_job(
            monkeypatch, access_token, expiring, other, delete_the_other_user
        )

        assert waiting is True, "the bulk delete finished before the job's batch let it go"
        assert not isinstance(outcome, BaseException), f"the bulk delete raised {outcome!r}"
        assert outcome.status_code == status.HTTP_200_OK, f"{outcome.status_code} {outcome.text}"
        assert outcome.json()["users"] == [other["username"]]
        assert deleted == [expiring["id"]]
        assert stored == {}
    finally:
        _drop_world(access_token, core, group, (expiring, other))


def test_an_admin_activating_the_next_plan_of_a_user_the_autodelete_job_holds_waits_for_it_instead_of_deadlocking(
    access_token, monkeypatch
):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")
    core, group, expiring, other = _wireguard_world(
        access_token, "10.49.3", {"next_plan": {"data_limit": 1024, "expire": 30 * 86400}}
    )
    try:

        async def activate_the_next_plan(racing_client, headers):
            return await racing_client.post(f"/api/user/by-id/{expiring['id']}/active_next", headers=headers)

        deleted, outcome, stored, waiting = _race_the_autodelete_job(
            monkeypatch, access_token, expiring, other, activate_the_next_plan
        )

        assert waiting is True, "the next plan activation finished before the job's batch let it go"
        assert not isinstance(outcome, BaseException), f"the next plan activation raised {outcome!r}"
        assert outcome.status_code == status.HTTP_404_NOT_FOUND, f"{outcome.status_code} {outcome.text}"
        assert deleted == [expiring["id"]]
        assert stored == {other["id"]: UserStatus.active}
    finally:
        _drop_world(access_token, core, group, (expiring, other))
