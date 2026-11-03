from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.db.crud import user as user_crud
from app.db.models import Admin, User, UserStatus
from app.fork.crud import admin_scope
from app.fork.models.connection import ConnectionRestriction
from app.fork.proxy_secrets.locks import driver_code
from app.models.admin import BulkAdminSelection
from app.operation import OperatorType, admin as admin_operation
from app.operation.admin import AdminOperation
from tests.test_admin_wide_owner_moves_interleavings import (  # noqa: F401
    ACTIONS,
    BOUND,
    OWNER,
    STAGGER_SECONDS,
    _admin_wide,
    _hold_after,
    _mover,
    _run,
    first_commits_fixture,
    pushes_fixture,
    quiet,
)
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401
from tests.test_status_lock_order_interleavings import _deadlocks

pytestmark = pytest.mark.skipif(_database_url().startswith("sqlite"), reason="needs a server database")

ORDERS = [list(order) for order in itertools.permutations(["admin-wide", "mover", "remover"])]
INTO_ORDERS = [["mover", "remover"], ["remover", "mover"]]
MYSQL_CHILD_ROW_FK = 1452
POSTGRES_FK_VIOLATION = "23503"


def _remover(recorder_db, admin_id: int):
    async def run():
        async with recorder_db() as db:
            db.info["label"] = "remover"
            await AdminOperation(OperatorType.API).bulk_remove_admins(db, BulkAdminSelection(ids={admin_id}), OWNER)

    return run


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ORDERS, ids=["-".join(order) for order in ORDERS])
@pytest.mark.parametrize("action", list(ACTIONS))
async def test_bulk_owner_move_of_many_users_while_admin_wide_change_and_admin_removal_run(
    monkeypatch: pytest.MonkeyPatch, recorder_db, pushes, first_commits, action, order
):
    method, before, after = ACTIONS[action]
    home, stay_ids, _ = await seed_users_and_nodes(recorder_db, user_count=4, node_count=0)
    away, away_ids, _ = await seed_users_and_nodes(recorder_db, user_count=30, node_count=0)
    gone, gone_ids, _ = await seed_users_and_nodes(recorder_db, user_count=6, node_count=0)
    everyone = [*stay_ids, *away_ids, *gone_ids]
    moved_ids = (*away_ids, *gone_ids)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id.in_(everyone))
            .values(status=before, used_traffic=1000, last_status_change=datetime.now(UTC) - timedelta(minutes=40))
        )
        await session.execute(update(Admin).where(Admin.id.in_([home, away, gone])).values(used_traffic=0))
        if before == UserStatus.disabled:
            session.add_all(
                ConnectionRestriction(
                    user_id=user_id,
                    created_at=datetime.now(UTC) - timedelta(minutes=30),
                    previous_status=UserStatus.active.value,
                    disable_minutes=30,
                    restore_at=datetime.now(UTC) + timedelta(minutes=30),
                    active=True,
                )
                for user_id in everyone
            )
        await session.commit()
        home_name = (await session.execute(select(Admin.username).where(Admin.id == home))).scalar_one()

    _hold_after(monkeypatch, admin_operation, "close_visible_restrictions")
    _hold_after(monkeypatch, user_crud, "lock_owner_rows")

    steps = {
        "admin-wide": _admin_wide(recorder_db, home, method),
        "mover": _mover(recorder_db, "mover", "bulk_set_owner", moved_ids, home_name),
        "remover": _remover(recorder_db, gone),
    }
    outcomes: dict[str, str] = {}
    deadlocks_before = await _deadlocks()
    tasks = []
    for label in order:
        tasks.append(asyncio.create_task(_run(label, steps[label], outcomes)))
        await asyncio.sleep(STAGGER_SECONDS)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=BOUND)
    await asyncio.sleep(1)
    deadlocks_after = await _deadlocks()

    assert all(not outcome.startswith("aborted") for outcome in outcomes.values()), outcomes
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, outcomes

    async with recorder_db() as session:
        rows = {
            user_id: (admin_id, status)
            for user_id, admin_id, status in (
                await session.execute(select(User.id, User.admin_id, User.status).where(User.id.in_(everyone)))
            ).all()
        }
        traffic = dict(
            (await session.execute(select(Admin.id, Admin.used_traffic).where(Admin.id.in_([home, away, gone])))).all()
        )
        open_restrictions = set(
            (
                await session.execute(
                    select(ConnectionRestriction.user_id).where(
                        ConnectionRestriction.user_id.in_(everyone), ConnectionRestriction.active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )

    if outcomes.get("admin-wide") == "committed":
        for user_id in stay_ids:
            assert rows[user_id] == (home, after), (user_id, rows[user_id], outcomes)
            assert user_id not in open_restrictions, (user_id, outcomes)
    if outcomes.get("mover") == "committed":
        for user_id in moved_ids:
            assert rows[user_id][0] == home, (user_id, rows[user_id], outcomes)
        statuses = {(rows[user_id][1], user_id in open_restrictions) for user_id in moved_ids}
        assert len(statuses) == 1, ("moved users were changed by part of the admin-wide change", statuses, outcomes)
        assert traffic[home] == 1000 * len(moved_ids), (traffic, outcomes)
        assert traffic[away] == -1000 * len(away_ids), (traffic, outcomes)
    if outcomes.get("remover") == "committed":
        assert gone not in traffic, (traffic, outcomes)
    for user_id, (_admin_id, status) in rows.items():
        if status != before:
            assert (user_id, status) in pushes, (user_id, status, outcomes)


def _target_admin_was_deleted(exc: BaseException) -> bool:
    if isinstance(exc, HTTPException):
        return exc.status_code == 404
    if not isinstance(exc, IntegrityError):
        return False
    sqlstates = {getattr(exc.orig, "sqlstate", None), getattr(exc.orig, "pgcode", None)}
    return driver_code(exc) == MYSQL_CHILD_ROW_FK or POSTGRES_FK_VIOLATION in sqlstates


async def _attempt(label: str, step, outcomes: dict[str, str], failures: dict[str, BaseException]) -> None:
    try:
        await step()
        outcomes[label] = "committed"
    except Exception as exc:
        failures[label] = exc
        outcomes[label] = f"failed: {type(exc).__name__}: {getattr(exc, 'orig', exc)!r}"[:300]


@pytest.mark.asyncio
@pytest.mark.parametrize("order", INTO_ORDERS, ids=["-".join(order) for order in INTO_ORDERS])
@pytest.mark.parametrize("mover", ["set_owner", "bulk_set_owner"])
async def test_users_moved_into_an_admin_while_it_is_removed_never_abort_the_removal(
    monkeypatch: pytest.MonkeyPatch, recorder_db, mover, order
):
    away, away_ids, _ = await seed_users_and_nodes(recorder_db, user_count=30, node_count=0)
    gone, gone_ids, _ = await seed_users_and_nodes(recorder_db, user_count=6, node_count=0)
    moved_ids = tuple(away_ids[:1]) if mover == "set_owner" else tuple(away_ids)
    everyone = [*away_ids, *gone_ids]
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id.in_(everyone)).values(used_traffic=1000))
        await session.execute(update(Admin).where(Admin.id.in_([away, gone])).values(used_traffic=0))
        await session.commit()
        gone_name = (await session.execute(select(Admin.username).where(Admin.id == gone))).scalar_one()

    _hold_after(monkeypatch, user_crud, "shift_admin_traffic")
    _hold_after(monkeypatch, admin_scope, "lock_user_rows")

    steps = {
        "mover": _mover(recorder_db, "mover", mover, moved_ids, gone_name),
        "remover": _remover(recorder_db, gone),
    }
    outcomes: dict[str, str] = {}
    failures: dict[str, BaseException] = {}
    deadlocks_before = await _deadlocks()
    tasks = []
    for label in order:
        tasks.append(asyncio.create_task(_attempt(label, steps[label], outcomes, failures)))
        await asyncio.sleep(STAGGER_SECONDS)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=BOUND)
    await asyncio.sleep(1)
    deadlocks_after = await _deadlocks()

    assert outcomes["remover"] == "committed", outcomes
    assert outcomes["mover"] == "committed" or _target_admin_was_deleted(failures["mover"]), outcomes
    if deadlocks_before is not None:
        assert deadlocks_after - deadlocks_before == 0, outcomes

    async with recorder_db() as session:
        owners = dict((await session.execute(select(User.id, User.admin_id).where(User.id.in_(everyone)))).all())
        used = dict((await session.execute(select(User.id, User.used_traffic).where(User.id.in_(everyone)))).all())
        traffic = dict(
            (await session.execute(select(Admin.id, Admin.used_traffic).where(Admin.id.in_([away, gone])))).all()
        )
        still_owned = (await session.execute(select(User.id).where(User.admin_id == gone))).scalars().all()

    assert gone not in traffic, (traffic, outcomes)
    assert still_owned == [], (still_owned, outcomes)
    assert all(owners[user_id] is None for user_id in gone_ids), (owners, outcomes)
    assert all(used[user_id] == 1000 for user_id in everyone), (used, outcomes)
    kept_ids = [user_id for user_id in away_ids if user_id not in moved_ids]
    assert all(owners[user_id] == away for user_id in kept_ids), (owners, outcomes)
    if outcomes["mover"] == "committed":
        assert all(owners[user_id] is None for user_id in moved_ids), (owners, outcomes)
        assert traffic[away] == -1000 * len(moved_ids), (traffic, outcomes)
    else:
        assert all(owners[user_id] == away for user_id in moved_ids), (owners, outcomes)
        assert traffic[away] == 0, (traffic, outcomes)
