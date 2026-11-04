from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, insert, select

from app.db.models import Admin, Group, User, UserTemplate, template_group_association
from app.models.admin import AdminDetails, AdminRoleData
from app.models.admin_role import RoleLimits
from app.models.user import BulkUsersFromTemplate, CreateUserFromTemplate, UserCreate, UsernameGenerationStrategy
from app.operation import OperatorType
from app.operation.user import UserOperation
from tests.test_admin_removal_during_owner_moves import _attempt
from tests.test_admin_wide_owner_moves_interleavings import BOUND, quiet  # noqa: F401
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks
from tests.test_user_create_vs_creator_removal import silent_sync  # noqa: F401

pytestmark = pytest.mark.skipif(_database_url().startswith("sqlite"), reason="needs a server database")

QUOTA = 3


async def _seed_reseller_one_below_quota(recorder_db) -> tuple[AdminDetails, int, int, str]:
    creator_id, _, _ = await seed_users_and_nodes(recorder_db, user_count=QUOTA - 1, node_count=0)
    tag = uuid4().hex[:8]
    async with recorder_db() as session:
        name = (await session.execute(select(Admin.username).where(Admin.id == creator_id))).scalar_one()
        group_id = (await session.execute(insert(Group).values(name=f"br2-{tag}"))).inserted_primary_key[0]
        template_id = (await session.execute(insert(UserTemplate).values(name=f"br2-{tag}"))).inserted_primary_key[0]
        await session.execute(
            insert(template_group_association).values(user_template_id=template_id, group_id=group_id)
        )
        await session.commit()
    reseller = AdminDetails(
        id=creator_id,
        username=name,
        role=AdminRoleData(id=2, name="reseller", is_owner=False, limits=RoleLimits(max_users=QUOTA)),
    )
    return reseller, group_id, template_id, f"br2{tag}"


def _create(recorder_db, kind: str, label: str, admin: AdminDetails, group_id: int, template_id: int, username: str):
    async def run():
        async with recorder_db() as db:
            db.info["label"] = label
            operation = UserOperation(OperatorType.API)
            if kind == "single":
                await operation.create_user(db, UserCreate(username=username, group_ids=[group_id]), admin)
            elif kind == "bulk":
                await operation.bulk_create_users_from_template(
                    db,
                    BulkUsersFromTemplate(
                        user_template_id=template_id,
                        count=1,
                        strategy=UsernameGenerationStrategy.sequence,
                        username=username,
                        start_number=1,
                    ),
                    admin,
                )
            else:
                await operation.create_user_from_template(
                    db, CreateUserFromTemplate(user_template_id=template_id, username=username), admin
                )

    return run


async def _owned_by(recorder_db, admin_id: int) -> int:
    async with recorder_db() as session:
        return int((await session.execute(select(func.count(User.id)).where(User.admin_id == admin_id))).scalar_one())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["single", "template", "bulk"])
async def test_a_create_that_counted_before_a_concurrent_create_committed_never_exceeds_max_users(
    monkeypatch: pytest.MonkeyPatch, recorder_db, kind
):
    reseller, group_id, template_id, base = await _seed_reseller_one_below_quota(recorder_db)
    late_counted = asyncio.Event()
    early_finished = asyncio.Event()
    gated_name = "_persist_bulk_users" if kind == "bulk" else "_prepare_user_proxy_settings"
    real_step = getattr(UserOperation, gated_name)

    async def gated_step(self, db, *args, **kwargs):
        if db.info.get("label") == "late":
            late_counted.set()
            await asyncio.wait_for(early_finished.wait(), BOUND)
        return await real_step(self, db, *args, **kwargs)

    monkeypatch.setattr(UserOperation, gated_name, gated_step)

    outcomes: dict[str, str] = {}
    failures: dict[str, BaseException] = {}
    deadlocks_before = await _deadlocks()
    late = asyncio.create_task(
        _attempt(
            "late",
            _create(recorder_db, kind, "late", reseller, group_id, template_id, f"{base}late"),
            outcomes,
            failures,
        )
    )
    await asyncio.wait_for(late_counted.wait(), BOUND)
    await _attempt(
        "early",
        _create(recorder_db, kind, "early", reseller, group_id, template_id, f"{base}early"),
        outcomes,
        failures,
    )
    early_finished.set()
    await asyncio.wait_for(late, BOUND)
    deadlocks_after = await _deadlocks()

    assert outcomes["early"] == "committed", (outcomes, failures)
    assert isinstance(failures.get("late"), HTTPException), (outcomes, failures)
    assert failures["late"].status_code == 400, (outcomes, failures)
    refusal = "Bulk create would exceed user limit" if kind == "bulk" else "User limit reached"
    assert failures["late"].detail.startswith(refusal), (outcomes, failures)
    assert await _owned_by(recorder_db, reseller.id) == QUOTA, outcomes
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, outcomes
