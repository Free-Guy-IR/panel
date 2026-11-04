from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db.models import Admin
from app.models.admin import AdminDetails, AdminRoleData
from app.models.admin_role import RoleLimits
from app.models.user import BulkUsersFromTemplate, UserCreate, UsernameGenerationStrategy
from app.operation import OperatorType, user as user_operation
from app.operation.user import UserOperation
from tests.test_admin_removal_during_owner_moves import _attempt
from tests.test_admin_wide_owner_moves_interleavings import BOUND, quiet  # noqa: F401
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks
from tests.test_user_create_vs_creator_removal import _created, _creator, _seed_creator, silent_sync  # noqa: F401

SERVER_ONLY = pytest.mark.skipif(_database_url().startswith("sqlite"), reason="needs a server database")
SETTLE_SECONDS = 0.6
QUOTA = 10


def _quota_bound(creator_id: int, name: str) -> AdminDetails:
    return AdminDetails(
        id=creator_id,
        username=name,
        role=AdminRoleData(id=2, name="reseller", is_owner=False, limits=RoleLimits(max_users=QUOTA)),
    )


def _single(recorder_db, admin: AdminDetails, group_id: int, username: str):
    async def run():
        async with recorder_db() as db:
            db.info["label"] = "single"
            await UserOperation(OperatorType.API).create_user(
                db, UserCreate(username=username, group_ids=[group_id]), admin
            )

    return run


def _bulk(recorder_db, admin: AdminDetails, template_id: int, base: str):
    async def run():
        async with recorder_db() as db:
            db.info["label"] = "bulk"
            await UserOperation(OperatorType.API).bulk_create_users_from_template(
                db,
                BulkUsersFromTemplate(
                    user_template_id=template_id,
                    count=2,
                    strategy=UsernameGenerationStrategy.sequence,
                    username=base,
                    start_number=1,
                ),
                admin,
            )

    return run


@SERVER_ONLY
@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["bulk", "single"])
async def test_a_quota_bound_bulk_create_and_a_single_create_by_the_same_admin_never_deadlock(
    monkeypatch: pytest.MonkeyPatch, recorder_db, first
):
    creator_id, name, group_id, template_id, base = await _seed_creator(recorder_db)
    bulk_started = asyncio.Event()
    bulk_holds_quota = asyncio.Event()
    single_started = asyncio.Event()
    single_holds_secrets = asyncio.Event()
    real_quota_lock = user_operation.lock_admin_quota_row
    real_prepare = UserOperation._prepare_user_proxy_settings
    real_persist = UserOperation._persist_bulk_users

    async def gated_persist(self, db, *args, **kwargs):
        bulk_started.set()
        return await real_persist(self, db, *args, **kwargs)

    async def gated_quota_lock(db, admin_id):
        await real_quota_lock(db, admin_id)
        bulk_holds_quota.set()
        if first == "bulk":
            await asyncio.wait_for(single_started.wait(), BOUND)
            await asyncio.sleep(SETTLE_SECONDS)

    async def gated_prepare(self, db, *args, **kwargs):
        if db.info.get("label") == "single":
            single_started.set()
        result = await real_prepare(self, db, *args, **kwargs)
        if db.info.get("label") == "single" and first == "single":
            single_holds_secrets.set()
            await asyncio.wait_for(bulk_started.wait(), BOUND)
            await asyncio.sleep(SETTLE_SECONDS)
        return result

    monkeypatch.setattr(UserOperation, "_persist_bulk_users", gated_persist)
    monkeypatch.setattr(user_operation, "lock_admin_quota_row", gated_quota_lock)
    monkeypatch.setattr(UserOperation, "_prepare_user_proxy_settings", gated_prepare)

    single = _single(recorder_db, _creator(creator_id, name), group_id, f"{base}s1")
    bulk = _bulk(recorder_db, _quota_bound(creator_id, name), template_id, f"{base}b")
    outcomes: dict[str, str] = {}
    failures: dict[str, BaseException] = {}
    deadlocks_before = await _deadlocks()
    if first == "bulk":
        leader = asyncio.create_task(_attempt("bulk", bulk, outcomes, failures))
        await asyncio.wait_for(bulk_holds_quota.wait(), BOUND)
        follower = asyncio.create_task(_attempt("single", single, outcomes, failures))
    else:
        leader = asyncio.create_task(_attempt("single", single, outcomes, failures))
        await asyncio.wait_for(single_holds_secrets.wait(), BOUND)
        follower = asyncio.create_task(_attempt("bulk", bulk, outcomes, failures))
    await asyncio.wait_for(asyncio.gather(leader, follower), timeout=BOUND)
    deadlocks_after = await _deadlocks()

    assert outcomes == {"bulk": "committed", "single": "committed"}, (outcomes, failures)
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, outcomes
    created = await _created(recorder_db, base)
    assert len(created) == 3 and f"{base}s1" in created, created
    assert set(created.values()) == {creator_id}, created


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["single", "bulk"])
async def test_a_create_whose_admin_username_now_names_another_admin_answers_404_and_stores_nothing(recorder_db, kind):
    _, name, group_id, template_id, base = await _seed_creator(recorder_db)
    async with recorder_db() as session:
        stale_id = (await session.execute(select(Admin.id).order_by(Admin.id.desc()).limit(1))).scalar_one() + 1000
    stale = _creator(stale_id, name)

    with pytest.raises(HTTPException) as answered:
        if kind == "single":
            await _single(recorder_db, stale, group_id, f"{base}s1")()
        else:
            await _bulk(recorder_db, stale, template_id, f"{base}b")()

    assert answered.value.status_code == 404
    assert await _created(recorder_db, base) == {}
