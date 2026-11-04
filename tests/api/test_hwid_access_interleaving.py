import asyncio

import pytest
from fastapi import HTTPException

from app.db.crud.hwid import get_user_hwids, register_user_hwid
from app.operation import OperatorType
from app.operation.hwid import HWIDOperation
from app.routers.authentication import get_admin
from tests.api import DATABASE_URL, client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin
from tests.api.test_permission_leaks import SCOPE_OWN, _create_role, _delete_role, _login
from tests.api.test_template_bulk_access_interleaving import _own_connection_sessions


@pytest.fixture
def hwid_actors(access_token):
    role = _create_role(
        access_token,
        {"users": {"read": SCOPE_OWN, "delete": SCOPE_OWN}, "hwids": {"read": True, "delete": True}},
    )
    actor = create_admin(access_token, role_id=role["id"])
    victim = create_user(access_token)
    try:
        yield {
            "actor_token": _login(actor["username"], actor["password"]),
            "owner_token": access_token,
            "victim": victim,
        }
    finally:
        client.delete(f"/api/user/by-id/{victim['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, actor["username"])
        _delete_role(access_token, role["id"])


@pytest.mark.skipif(":memory:" in DATABASE_URL, reason="needs a database shared by several connections")
def test_concurrent_restricted_hwid_actions_never_touch_another_admins_user(hwid_actors):
    victim_id = hwid_actors["victim"]["id"]
    operation = HWIDOperation(operator_type=OperatorType.API)
    seeded = [f"seed-{index}" for index in range(4)]
    added = [f"owner-{index}" for index in range(6)]
    removed = {"seed-0", "owner-1"}

    async def _scenario() -> tuple[list[str], set[str]]:
        engine, sessions = _own_connection_sessions()
        async with sessions() as db:
            actor = await get_admin(db, hwid_actors["actor_token"])
            owner = await get_admin(db, hwid_actors["owner_token"])
            for hwid in seeded:
                await register_user_hwid(db, victim_id, hwid)
            await db.commit()
        assert actor is not None and owner is not None

        async def _restricted(step: int) -> str:
            await asyncio.sleep(0.001 * step)
            async with sessions() as db:
                try:
                    if step % 3 == 0:
                        await operation.reset_user_hwids(db, victim_id, actor)
                    elif step % 3 == 1:
                        await operation.delete_user_hwid(db, victim_id, seeded[step % len(seeded)], actor)
                    else:
                        await operation.get_user_hwids(db, victim_id, actor)
                    return "ok"
                except HTTPException as exc:
                    await db.rollback()
                    return f"{exc.status_code}:{exc.detail}"

        async def _owner_adds(step: int) -> str:
            await asyncio.sleep(0.0015 * step)
            async with sessions() as db:
                await register_user_hwid(db, victim_id, added[step])
                await db.commit()
                return "added"

        async def _owner_removes(hwid: str, step: int) -> str:
            await asyncio.sleep(0.002 * step + 0.012)
            async with sessions() as db:
                await operation.delete_user_hwid(db, victim_id, hwid, owner)
                return "removed"

        try:
            results = await asyncio.gather(
                *(_restricted(step) for step in range(12)),
                *(_owner_adds(step) for step in range(len(added))),
                *(_owner_removes(hwid, step) for step, hwid in enumerate(sorted(removed))),
            )
            async with sessions() as db:
                remaining = {row.hwid for row in await get_user_hwids(db, victim_id)}
        finally:
            await engine.dispose()
        return list(results), remaining

    results, remaining = asyncio.run(_scenario())

    assert results[:12] == ["404:User not found"] * 12
    assert results[12:] == ["added"] * len(added) + ["removed"] * len(removed)
    assert remaining == (set(seeded) | set(added)) - removed
