from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, User
from app.fork.crud.group_scope import user_in_allowed_groups
from app.fork.crud.user_locks import USER_LOCK_BATCH_SIZE, lock_user_rows, on_primary_key
from app.fork.proxy_secrets.locks import read_committed_rows


def _admin_users(admin_id: int, allowed_group_ids: list[int] | None):
    stmt = select(User.id).where(User.admin_id == admin_id)
    if allowed_group_ids is not None:
        stmt = stmt.where(user_in_allowed_groups(User.id, allowed_group_ids))
    return stmt


def id_batches(user_ids: list[int]) -> list[list[int]]:
    ordered = sorted(set(user_ids))
    return [ordered[start : start + USER_LOCK_BATCH_SIZE] for start in range(0, len(ordered), USER_LOCK_BATCH_SIZE)]


def locked_targets(statement, user_ids: list[int] | None) -> list:
    if user_ids is None:
        return [statement]
    return [on_primary_key(statement).where(User.id.in_(batch)) for batch in id_batches(user_ids)]


async def lock_admin_users(db: AsyncSession, admin_id: int, allowed_group_ids: list[int] | None) -> list[int]:
    candidates = [user_id for (user_id,) in await read_committed_rows(db, _admin_users(admin_id, allowed_group_ids))]
    await lock_user_rows(db, candidates)
    locked: list[int] = []
    for batch in id_batches(candidates):
        stmt = _admin_users(admin_id, allowed_group_ids).where(User.id.in_(batch))
        locked.extend(user_id for (user_id,) in await read_committed_rows(db, stmt))
    return sorted(locked)


async def _detach_committed_owners(db: AsyncSession, admin_ids: list[int]) -> None:
    owned = select(User.id).where(User.admin_id.in_(admin_ids))
    candidates = [user_id for (user_id,) in await read_committed_rows(db, owned)]
    await lock_user_rows(db, candidates)
    for statement in locked_targets(update(User).where(User.admin_id.in_(admin_ids)), candidates):
        await db.execute(statement.values(admin_id=None))


async def detach_admin_users(db: AsyncSession, admin_ids: list[int]) -> None:
    await _detach_committed_owners(db, admin_ids)
    await db.execute(select(Admin.id).where(Admin.id.in_(admin_ids)).order_by(Admin.id).with_for_update())
    await _detach_committed_owners(db, admin_ids)


async def count_visible_admin_users(db: AsyncSession, admin_id: int, allowed_group_ids: list[int]) -> int:
    stmt = select(func.count(User.id)).where(
        User.admin_id == admin_id, user_in_allowed_groups(User.id, allowed_group_ids)
    )
    return int((await db.execute(stmt)).scalar() or 0)
