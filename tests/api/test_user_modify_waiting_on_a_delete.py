import asyncio

import httpx
import pytest
from fastapi import status
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import base
from app.db.crud import user as user_crud
from tests.api import DATABASE_URL, IS_SQLITE, app, client
from tests.api.helpers import auth_headers, create_user, unique_name
from tests.api.lock_waits import a_lock_wait_appears

HOLD_SECONDS = 0.5


def test_a_user_modify_that_waited_for_the_user_to_be_deleted_answers_not_found(access_token, monkeypatch):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")
    user = create_user(access_token, payload={"username": unique_name("modify_after_delete")})
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {})
    session_factory = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)
    real_dependencies = user_crud._delete_user_dependencies
    headers = auth_headers(access_token)
    race = {"client": None, "request": None, "waiting": None}

    async def delete_dependencies_while_a_modify_waits(db, user_ids):
        if race["request"] is None:
            race["request"] = asyncio.create_task(
                race["client"].put(f"/api/user/by-id/{user['id']}", headers=headers, json={"note": "renewed"})
            )
            observed = await a_lock_wait_appears(engine, HOLD_SECONDS)
            race["waiting"] = observed if observed is not None else not race["request"].done()
        return await real_dependencies(db, user_ids)

    async def racing_db():
        async with session_factory() as db:
            yield db

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as racing_client:
            race["client"] = racing_client
            deleted = await racing_client.delete(f"/api/user/by-id/{user['id']}", headers=headers)
            outcome = (await asyncio.gather(race["request"], return_exceptions=True))[0]
        await engine.dispose()
        return deleted, outcome

    monkeypatch.setattr(user_crud, "_delete_user_dependencies", delete_dependencies_while_a_modify_waits)
    previous_get_db = app.dependency_overrides[base.get_db]
    app.dependency_overrides[base.get_db] = racing_db
    try:
        deleted, outcome = asyncio.run(run())
    finally:
        app.dependency_overrides[base.get_db] = previous_get_db
        monkeypatch.setattr(user_crud, "_delete_user_dependencies", real_dependencies)
        leftover = client.delete(f"/api/user/by-id/{user['id']}", headers=headers)

    assert deleted.status_code == status.HTTP_204_NO_CONTENT, deleted.text
    assert race["waiting"] is True, "the modify finished before the delete let it go"
    assert not isinstance(outcome, BaseException), f"the modify raised {outcome!r}"
    assert outcome.status_code == status.HTTP_404_NOT_FOUND, f"{outcome.status_code} {outcome.text}"
    assert outcome.json()["detail"] == "User not found"
    assert leftover.status_code == status.HTTP_404_NOT_FOUND, leftover.text
