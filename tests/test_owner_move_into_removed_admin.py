from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, User
from app.fork.crud.owner_traffic import lock_destination_admin
from app.models.admin import BulkAdminSelection
from app.models.user import BulkUsersSetOwner
from app.operation import OperatorType, user as user_operation
from app.operation.admin import AdminOperation
from app.operation.user import UserOperation
from tests.test_admin_wide_owner_moves_interleavings import OWNER, quiet  # noqa: F401
from tests.test_recorder_harness import (  # noqa: F401
    _database_url,
    _set_sqlite_foreign_keys,
    recorder_db_fixture,
    seed_users_and_nodes,
)

pytestmark = pytest.mark.skipif(not _database_url().startswith("sqlite"), reason="drives the SQLite path")


def _remove_before(monkeypatch: pytest.MonkeyPatch, recorder_db, name: str, admin_id: int) -> None:
    real = getattr(user_operation, name)

    async def removed_first(*args, **kwargs):
        async with recorder_db() as db:
            await AdminOperation(OperatorType.API).bulk_remove_admins(db, BulkAdminSelection(ids={admin_id}), OWNER)
        return await real(*args, **kwargs)

    monkeypatch.setattr(user_operation, name, removed_first)


@pytest.mark.asyncio
@pytest.mark.parametrize("mover", ["set_owner", "bulk_set_owner"])
async def test_users_are_not_moved_onto_an_admin_removed_after_the_move_was_validated(
    monkeypatch: pytest.MonkeyPatch, recorder_db, mover
):
    await _set_sqlite_foreign_keys(recorder_db.kw["bind"], False)
    away, away_ids, _ = await seed_users_and_nodes(recorder_db, user_count=30, node_count=0)
    gone, gone_ids, _ = await seed_users_and_nodes(recorder_db, user_count=6, node_count=0)
    moved_ids = away_ids[:1] if mover == "set_owner" else away_ids
    everyone = [*away_ids, *gone_ids]
    async with recorder_db() as session:
        await session.execute(update(User).where(User.id.in_(everyone)).values(used_traffic=1000))
        await session.execute(update(Admin).where(Admin.id.in_([away, gone])).values(used_traffic=0))
        await session.commit()
        gone_name = (await session.execute(select(Admin.username).where(Admin.id == gone))).scalar_one()

    _remove_before(monkeypatch, recorder_db, mover, gone)

    operation = UserOperation(OperatorType.API)
    with pytest.raises(HTTPException) as raised:
        async with recorder_db() as db:
            if mover == "set_owner":
                await operation.set_owner_by_id(db, moved_ids[0], gone_name, OWNER)
            else:
                await operation.bulk_set_owner(
                    db, BulkUsersSetOwner(ids=set(moved_ids), admin_username=gone_name), OWNER
                )
    assert raised.value.status_code == 404

    async with recorder_db() as session:
        owners = dict((await session.execute(select(User.id, User.admin_id).where(User.id.in_(everyone)))).all())
        traffic = dict(
            (await session.execute(select(Admin.id, Admin.used_traffic).where(Admin.id.in_([away, gone])))).all()
        )
        still_owned = (await session.execute(select(User.id).where(User.admin_id == gone))).scalars().all()

    assert still_owned == []
    assert gone not in traffic
    assert traffic[away] == 0
    assert all(owners[user_id] == away for user_id in away_ids), owners
    assert all(owners[user_id] is None for user_id in gone_ids), owners


@pytest.mark.asyncio
async def test_the_destination_admin_lock_holds_the_sqlite_write_lock_until_the_move_ends(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'owner-move.sqlite3'}"
    mover_engine = create_async_engine(url, connect_args={"check_same_thread": False, "timeout": 0})
    remover_engine = create_async_engine(url, connect_args={"check_same_thread": False, "timeout": 0})
    async with mover_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(mover_engine, expire_on_commit=False)() as session:
        role = AdminRole(name="owner-move-role")
        session.add(role)
        await session.flush()
        admin = Admin(username="owner-move-target", hashed_password="x", role_id=role.id)
        session.add(admin)
        await session.commit()
        admin_id = admin.id

    try:
        async with async_sessionmaker(mover_engine, expire_on_commit=False)() as mover:
            await lock_destination_admin(mover, admin_id, {})
            with pytest.raises(OperationalError, match="locked"):
                async with remover_engine.begin() as conn:
                    await conn.execute(delete(Admin).where(Admin.id == admin_id))
            await mover.rollback()
        async with remover_engine.begin() as conn:
            assert (await conn.execute(delete(Admin).where(Admin.id == admin_id))).rowcount == 1
    finally:
        await mover_engine.dispose()
        await remover_engine.dispose()
