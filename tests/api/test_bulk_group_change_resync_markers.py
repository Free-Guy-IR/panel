import pytest
from fastapi import status
from sqlalchemy import select

from app.fork.models.pending_user_resync import PendingUserResync
from app.fork.proxy_secrets import group_repair
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)


async def _marked(user_ids: list[int]) -> list[int]:
    async with TestSession() as session:
        rows = await session.execute(
            select(PendingUserResync.user_id)
            .where(PendingUserResync.user_id.in_(user_ids))
            .order_by(PendingUserResync.user_id)
        )
        return list(rows.scalars().all())


class _TestGetDB:
    def __init__(self):
        self.db = TestSession()

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            await self.db.rollback()
        await self.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [0, 1])
@pytest.mark.parametrize("action", ["add", "remove"])
async def test_a_bulk_group_change_leaves_no_resync_marker_on_users_it_did_not_change(
    access_token, action, changed, monkeypatch
):
    core = create_core(access_token)
    member_group = create_group(access_token, name=unique_name("marker_member"))
    other_group = create_group(access_token, name=unique_name("marker_other"))
    target = member_group if action == "add" else other_group
    users = [
        create_user(access_token, group_ids=[member_group["id"]], payload={"username": unique_name("marker_user")})
        for _ in range(3)
    ]
    for user in users[:changed]:
        groups = [other_group["id"]] if action == "add" else [member_group["id"], other_group["id"]]
        response = client.put(
            f"/api/user/{user['username']}", headers=auth_headers(access_token), json={"group_ids": groups}
        )
        assert response.status_code == status.HTTP_200_OK, response.text
    user_ids = [user["id"] for user in users]
    pushed: list[list[int]] = []

    async def record_push(pushed_users):
        pushed.append(sorted(user.id for user in pushed_users))

    monkeypatch.setattr(group_repair, "GetDB", _TestGetDB)
    monkeypatch.setattr(group_repair, "sync_users", record_push)
    monkeypatch.setattr("app.operation.group.sync_users", record_push)
    try:
        assert await _marked(user_ids) == []
        response = client.post(
            f"/api/groups/bulk/{action}",
            headers=auth_headers(access_token),
            json={"group_ids": [target["id"]], "users": user_ids},
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        assert pushed == ([sorted(user_ids[:changed])] if changed else [])
        left = await _marked(user_ids)
        pushed.clear()
        drained = await group_repair.drain_pending_resyncs(min_age_seconds=0)

        assert left == [], f"a bulk {action} that changed {changed} of 3 users left resync markers on {left}"
        assert (drained, pushed) == (0, []), "the drain job re-pushed users the bulk change did not touch"
    finally:
        for user in users:
            delete_user(access_token, user["username"])
        delete_group(access_token, member_group["id"])
        delete_group(access_token, other_group["id"])
        delete_core(access_token, core["id"])
