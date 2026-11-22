import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import status
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import base
from tests.api import DATABASE_URL, IS_SQLITE, app, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    create_user_template,
    delete_core,
    delete_group,
    delete_user_template,
    get_inbounds,
    unique_name,
)

REACH_SECONDS = 20


def _l2tp_core(access_token, tag):
    response = client.post(
        "/api/core",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("renewal_l2tp"),
            "type": "l2tp",
            "config": {
                "inbound_tag": tag,
                "server_addr": "203.0.113.11",
                "pool": "10.32.0.0/24",
                "psk": "correct-horse-battery",
            },
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _l2tp_password(access_token, username):
    response = client.get(f"/api/user/{username}", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK, response.text
    return ((response.json().get("proxy_settings") or {}).get("l2tp") or {}).get("password")


def _give_next_plan(access_token, username, template_id):
    response = client.put(
        f"/api/user/{username}",
        headers=auth_headers(access_token),
        json={"next_plan": {"user_template_id": template_id}},
    )
    assert response.status_code == status.HTTP_200_OK, response.text


def test_a_renewal_that_read_the_user_before_its_plan_group_gained_l2tp_still_grants_the_password(
    access_token, monkeypatch
):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")
    from app.operation.user import UserOperation

    l2tp_tag = unique_name("rl2tp")
    xray_core = create_core(access_token)
    l2tp_core = _l2tp_core(access_token, l2tp_tag)
    xray_tag = next(tag for tag in get_inbounds(access_token) if tag != l2tp_tag)
    start_group = create_group(access_token, name=unique_name("renewal_start"), inbound_tags=[xray_tag])
    plan_group = create_group(access_token, name=unique_name("renewal_plan"), inbound_tags=[xray_tag])
    template = create_user_template(access_token, group_ids=[plan_group["id"]], expire_duration=86400 * 30)
    raced = create_user(access_token, group_ids=[start_group["id"]], payload={"username": unique_name("renew_raced")})
    control = create_user(
        access_token, group_ids=[start_group["id"]], payload={"username": unique_name("renew_control")}
    )
    for user in (raced, control):
        _give_next_plan(access_token, user["username"], template["id"])
    racing_engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {}
    )
    racing_session = async_sessionmaker(racing_engine, autoflush=False, expire_on_commit=False)

    async def racing_db():
        async with racing_session() as db:
            yield db

    real_active_next = UserOperation._active_next_plan
    gate = {"loaded": None, "edited": None}

    async def read_then_wait_for_the_edit(self, db, db_user, admin):
        if db_user is not None and db_user.username == raced["username"]:
            gate["loaded"].set()
            await asyncio.wait_for(gate["edited"].wait(), REACH_SECONDS)
        return await real_active_next(self, db, db_user, admin)

    monkeypatch.setattr(UserOperation, "_active_next_plan", read_then_wait_for_the_edit)
    monkeypatch.setattr("app.operation.group.sync_users", AsyncMock())
    headers = auth_headers(access_token)

    async def run():
        gate["loaded"], gate["edited"] = asyncio.Event(), asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as racing_client:
            renewing = asyncio.create_task(
                racing_client.post(f"/api/user/{raced['username']}/active_next", headers=headers)
            )
            await asyncio.wait_for(gate["loaded"].wait(), REACH_SECONDS)
            edited = await racing_client.put(
                f"/api/group/{plan_group['id']}",
                headers=headers,
                json={"name": plan_group["name"], "inbound_tags": [xray_tag, l2tp_tag]},
            )
            gate["edited"].set()
            return edited, await renewing

    previous_get_db = app.dependency_overrides[base.get_db]
    app.dependency_overrides[base.get_db] = racing_db
    try:
        edited, renewal = asyncio.run(run())
    finally:
        app.dependency_overrides[base.get_db] = previous_get_db
        monkeypatch.setattr(UserOperation, "_active_next_plan", real_active_next)
        asyncio.run(racing_engine.dispose())
    try:
        assert edited.status_code == status.HTTP_200_OK, edited.text
        assert renewal.status_code == status.HTTP_200_OK, renewal.text
        assert renewal.json()["group_ids"] == [plan_group["id"]], renewal.text
        renewed_after = client.post(f"/api/user/{control['username']}/active_next", headers=headers)
        assert renewed_after.status_code == status.HTTP_200_OK, renewed_after.text
        assert _l2tp_password(access_token, control["username"]), "control: a renewal after the edit grants L2TP"
        assert _l2tp_password(access_token, raced["username"]), (
            "a renewal that read the user before its plan group gained L2TP moved it into the group without an "
            "L2TP password"
        )
    finally:
        for user in (raced, control):
            client.delete(f"/api/user/{user['username']}", headers=headers)
        delete_user_template(access_token, template["id"])
        delete_group(access_token, plan_group["id"])
        delete_group(access_token, start_group["id"])
        delete_core(access_token, l2tp_core["id"])
        delete_core(access_token, xray_core["id"])
