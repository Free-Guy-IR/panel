from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

import app.db.crud.user as crud_user
from app.db.models import Admin, User
from app.node import sync as node_sync
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    create_group,
    create_user,
    delete_admin,
    delete_core,
    delete_group,
    unique_name,
)


def _recorder(monkeypatch):
    dispatched = []

    async def one(proto_user):
        dispatched.append(proto_user)

    async def many(proto_users):
        dispatched.extend(proto_users)

    monkeypatch.setattr(node_sync, "_dispatch_user_update", one)
    monkeypatch.setattr(node_sync, "_dispatch_users_update", many)
    return dispatched


def _last_push(dispatched, user_id):
    pushes = [p for p in dispatched if p.email == str(user_id)]
    return pushes[-1] if pushes else None


async def _exists(user_id: int) -> bool:
    async with TestSession() as session:
        return (await session.execute(select(User.id).where(User.id == user_id))).first() is not None


async def test_cleanup_of_on_hold_users_also_removes_them_from_the_nodes(access_token, monkeypatch):
    dispatched = _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("onhold_group"))
    owner = create_admin(access_token, role_id=3)
    user = create_user(
        access_token,
        group_ids=[group["id"]],
        payload={
            "username": unique_name("onhold_cleanup"),
            "status": "on_hold",
            "on_hold_expire_duration": 86400 * 30,
            "on_hold_timeout": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
    )
    try:
        async with TestSession() as session:
            owner_id = (await session.execute(select(Admin.id).where(Admin.username == owner["username"]))).scalar_one()
            await session.execute(update(User).where(User.id == user["id"]).values(admin_id=owner_id))
            await session.commit()
        on_node_before = list(_last_push(dispatched, user["id"]).inbounds)
        response = client.delete(
            "/api/users/expired",
            headers=auth_headers(access_token),
            params={"target": "on_hold", "admin_username": owner["username"]},
        )
        assert response.status_code == 200 and user["username"] in response.json()["users"]
        assert not await _exists(user["id"])
        last = _last_push(dispatched, user["id"])
        assert last is not None and not list(last.inbounds), (
            f"on_hold user was on the node with inbounds {on_node_before}, was deleted from the DB, "
            f"and the last state the node got still has inbounds {list(last.inbounds) if last else None}"
        )
    finally:
        client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, owner["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


async def test_a_bulk_delete_that_fails_half_way_still_removes_the_committed_users_from_nodes(
    access_token, monkeypatch
):
    dispatched = _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("bulkdel_group"))
    first = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("bulkdel_a")})
    second = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("bulkdel_b")})
    monkeypatch.setattr(crud_user, "DELETE_CHUNK_SIZE", 1)
    original = crud_user._delete_user_dependencies
    calls = {"n": 0}

    async def fail_on_second_chunk(db, user_ids):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Deadlock found when trying to get lock; try restarting transaction")
        return await original(db, user_ids)

    monkeypatch.setattr(crud_user, "_delete_user_dependencies", fail_on_second_chunk)
    try:
        try:
            outcome = client.post(
                "/api/users/bulk/delete",
                headers=auth_headers(access_token),
                json={"ids": [first["id"], second["id"]]},
            ).status_code
        except RuntimeError as exc:
            outcome = f"raised {exc}"
        gone = [u["id"] for u in (first, second) if not await _exists(u["id"])]
        still_live_on_node = [
            uid for uid in gone if (p := _last_push(dispatched, uid)) is not None and list(p.inbounds)
        ]
        assert not still_live_on_node, (
            f"request outcome={outcome!r}; users deleted from DB={gone}; of those, the node still holds "
            f"{still_live_on_node} with inbounds and no removal was ever pushed"
        )
    finally:
        monkeypatch.setattr(crud_user, "_delete_user_dependencies", original)
        for u in (first, second):
            client.delete(f"/api/user/by-id/{u['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
