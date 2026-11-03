from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, insert, select

from app import notification
from app.db.crud import user as user_crud
from app.db.models import Admin, Group, User, UserTemplate, template_group_association
from app.fork.crud import admin_scope
from app.models.admin import AdminDetails, AdminRoleData
from app.models.user import BulkUsersFromTemplate, UserCreate, UsernameGenerationStrategy
from app.operation import OperatorType, user as user_operation
from app.operation.user import UserOperation
from tests.test_admin_removal_during_owner_moves import _attempt, _remover
from tests.test_admin_wide_owner_moves_interleavings import BOUND, quiet  # noqa: F401
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks

SERVER_ONLY = pytest.mark.skipif(_database_url().startswith("sqlite"), reason="needs a server database")
SETTLE_SECONDS = 0.6
CREATES = ["single", "bulk"]


@pytest.fixture(autouse=True)
def silent_sync(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(user_operation, "sync_user", AsyncMock())
    monkeypatch.setattr(user_operation, "sync_users", AsyncMock())
    monkeypatch.setattr(notification, "create_user", AsyncMock())


async def _seed_creator(recorder_db) -> tuple[int, str, int, int, str]:
    creator_id, _, _ = await seed_users_and_nodes(recorder_db, user_count=0, node_count=0)
    tag = uuid4().hex[:8]
    async with recorder_db() as session:
        name = (await session.execute(select(Admin.username).where(Admin.id == creator_id))).scalar_one()
        group_id = (await session.execute(insert(Group).values(name=f"fo3-{tag}").returning(Group.id))).scalar_one()
        template_id = (
            await session.execute(insert(UserTemplate).values(name=f"fo3-{tag}").returning(UserTemplate.id))
        ).scalar_one()
        await session.execute(
            insert(template_group_association).values(user_template_id=template_id, group_id=group_id)
        )
        await session.commit()
    return creator_id, name, group_id, template_id, f"fo3{tag}"


def _creator(creator_id: int, name: str) -> AdminDetails:
    return AdminDetails(id=creator_id, username=name, role=AdminRoleData(id=1, name="owner", is_owner=True))


def _create(recorder_db, kind: str, admin: AdminDetails, group_id: int, template_id: int, base: str):
    async def run():
        async with recorder_db() as db:
            db.info["label"] = "creator"
            operation = UserOperation(OperatorType.API)
            if kind == "single":
                await operation.create_user(db, UserCreate(username=f"{base}1", group_ids=[group_id]), admin)
            else:
                await operation.bulk_create_users_from_template(
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


async def _created(recorder_db, base: str) -> dict[str, int | None]:
    async with recorder_db() as session:
        rows = await session.execute(select(User.username, User.admin_id).where(User.username.like(f"{base}%")))
        return dict(rows.all())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", CREATES)
async def test_a_create_by_an_admin_removed_before_it_answers_404_and_stores_no_ownerless_user(recorder_db, kind):
    creator_id, name, group_id, template_id, base = await _seed_creator(recorder_db)
    async with recorder_db() as session:
        await session.execute(delete(Admin).where(Admin.id == creator_id))
        await session.commit()

    with pytest.raises(HTTPException) as answered:
        await _create(recorder_db, kind, _creator(creator_id, name), group_id, template_id, base)()

    assert answered.value.status_code == 404
    assert await _created(recorder_db, base) == {}


@SERVER_ONLY
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", CREATES)
async def test_a_create_that_waits_on_a_removal_holding_its_creator_answers_404_and_stores_nothing(
    monkeypatch: pytest.MonkeyPatch, recorder_db, kind
):
    creator_id, name, group_id, template_id, base = await _seed_creator(recorder_db)
    remover_holds_admin = asyncio.Event()
    creator_prepared = asyncio.Event()
    passes: list[int] = []
    real_detach = admin_scope._detach_committed_owners
    real_prepare = UserOperation._prepare_user_proxy_settings

    async def gated_detach(db, admin_ids, *args, **kwargs):
        passes.append(len(passes) + 1)
        if len(passes) == 2:
            remover_holds_admin.set()
            await asyncio.wait_for(creator_prepared.wait(), BOUND)
            await asyncio.sleep(SETTLE_SECONDS)
        return await real_detach(db, admin_ids, *args, **kwargs)

    async def gated_prepare(self, db, *args, **kwargs):
        result = await real_prepare(self, db, *args, **kwargs)
        if db.info.get("label") == "creator":
            creator_prepared.set()
        return result

    monkeypatch.setattr(admin_scope, "_detach_committed_owners", gated_detach)
    monkeypatch.setattr(UserOperation, "_prepare_user_proxy_settings", gated_prepare)

    outcomes: dict[str, str] = {}
    failures: dict[str, BaseException] = {}
    deadlocks_before = await _deadlocks()
    remover = asyncio.create_task(_attempt("remover", _remover(recorder_db, creator_id), outcomes, failures))
    await asyncio.wait_for(remover_holds_admin.wait(), BOUND)
    creator = asyncio.create_task(
        _attempt(
            "creator",
            _create(recorder_db, kind, _creator(creator_id, name), group_id, template_id, base),
            outcomes,
            failures,
        )
    )
    await asyncio.wait_for(asyncio.gather(remover, creator), timeout=BOUND)
    deadlocks_after = await _deadlocks()

    assert outcomes["remover"] == "committed", outcomes
    assert isinstance(failures.get("creator"), HTTPException), outcomes
    assert failures["creator"].status_code == 404, (failures["creator"].detail, outcomes)
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, outcomes
    assert await _created(recorder_db, base) == {}
    async with recorder_db() as session:
        assert (await session.execute(select(Admin.id).where(Admin.id == creator_id))).scalars().all() == []


@SERVER_ONLY
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", CREATES)
async def test_a_removal_that_waits_on_a_create_holding_its_creator_detaches_the_new_users(
    monkeypatch: pytest.MonkeyPatch, recorder_db, kind
):
    creator_id, name, group_id, template_id, base = await _seed_creator(recorder_db)
    creator_inserted = asyncio.Event()

    def gate(real):
        async def gated(db, *args, **kwargs):
            result = await real(db, *args, **kwargs)
            if db.info.get("label") == "creator" and not creator_inserted.is_set():
                creator_inserted.set()
                await asyncio.sleep(SETTLE_SECONDS)
            return result

        return gated

    monkeypatch.setattr(user_crud, "sync_user_allocations", gate(user_crud.sync_user_allocations))
    monkeypatch.setattr(user_crud, "sync_users_allocations", gate(user_crud.sync_users_allocations))

    outcomes: dict[str, str] = {}
    failures: dict[str, BaseException] = {}
    deadlocks_before = await _deadlocks()
    creator = asyncio.create_task(
        _attempt(
            "creator",
            _create(recorder_db, kind, _creator(creator_id, name), group_id, template_id, base),
            outcomes,
            failures,
        )
    )
    await asyncio.wait_for(creator_inserted.wait(), BOUND)
    remover = asyncio.create_task(_attempt("remover", _remover(recorder_db, creator_id), outcomes, failures))
    await asyncio.wait_for(asyncio.gather(creator, remover), timeout=BOUND)
    deadlocks_after = await _deadlocks()

    assert outcomes == {"creator": "committed", "remover": "committed"}, outcomes
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, outcomes
    created = await _created(recorder_db, base)
    assert len(created) == (1 if kind == "single" else 2), created
    assert all(admin_id is None for admin_id in created.values()), created
    async with recorder_db() as session:
        assert (await session.execute(select(Admin.id).where(Admin.id == creator_id))).scalars().all() == []
