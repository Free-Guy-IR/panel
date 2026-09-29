from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import case, inspect, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models import NextPlan, User, UserStatus

USER_STATE_KEYS = (
    "status",
    "used_traffic",
    "data_limit",
    "_expire",
    "on_hold_expire_duration",
    "on_hold_timeout",
    "proxy_settings",
)
MODIFY_STATUS_KEYS = ("status", "used_traffic", "data_limit", "_expire", "on_hold_expire_duration")
USER_LOCK_BATCH_SIZE = 500


class NextPlanUnavailable(LookupError):
    pass


def _set_unmodified(db_user: User, values: dict[str, object]) -> None:
    state = inspect(db_user)
    for key, value in values.items():
        if not state.attrs[key].history.has_changes():
            set_committed_value(db_user, key, value)


async def refresh_user_state_for_update(db: AsyncSession, db_user: User, keys: Sequence[str] = USER_STATE_KEYS) -> bool:
    if db_user.id is None:
        return True
    columns = [User.__mapper__.attrs[key].columns[0] for key in keys]
    row = (await db.execute(select(*columns).where(User.id == db_user.id).with_for_update())).one_or_none()
    if row is None:
        return False
    _set_unmodified(db_user, dict(zip(keys, row, strict=True)))
    return True


async def lock_user_rows(db: AsyncSession, user_ids: Iterable[int]) -> None:
    ordered_ids = sorted({int(user_id) for user_id in user_ids if user_id is not None})
    for start in range(0, len(ordered_ids), USER_LOCK_BATCH_SIZE):
        chunk = ordered_ids[start : start + USER_LOCK_BATCH_SIZE]
        await db.execute(select(User.id).where(User.id.in_(chunk)).order_by(User.id).with_for_update())


async def refresh_users_state_for_update(
    db: AsyncSession, users: Iterable[User], keys: Sequence[str] = MODIFY_STATUS_KEYS
) -> list[User]:
    by_id = {db_user.id: db_user for db_user in users if db_user.id is not None}
    ordered_ids = sorted(by_id)
    columns = [User.__mapper__.attrs[key].columns[0] for key in keys]
    present: set[int] = set()
    for start in range(0, len(ordered_ids), USER_LOCK_BATCH_SIZE):
        chunk = ordered_ids[start : start + USER_LOCK_BATCH_SIZE]
        rows = await db.execute(select(User.id, *columns).where(User.id.in_(chunk)).order_by(User.id).with_for_update())
        for user_id, *values in rows.all():
            _set_unmodified(by_id[user_id], dict(zip(keys, values, strict=True)))
            present.add(user_id)
    return [db_user for db_user in by_id.values() if db_user.id in present]


async def refresh_next_plan_for_update(db: AsyncSession, db_user: User) -> NextPlan | None:
    plan = (
        (
            await db.execute(
                select(NextPlan)
                .where(NextPlan.user_id == db_user.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    set_committed_value(db_user, "next_plan", plan)
    return plan


def _reactivated_status():
    status_type = User.__table__.c.status.type
    return case(
        (User.status == UserStatus.limited, literal(UserStatus.active, status_type)),
        else_=User.status,
    )


async def reactivate_limited_users(db: AsyncSession, users: Iterable[User]) -> None:
    by_id = {db_user.id: db_user for db_user in users if db_user.id is not None}
    ordered_ids = sorted(by_id)
    for start in range(0, len(ordered_ids), USER_LOCK_BATCH_SIZE):
        chunk = ordered_ids[start : start + USER_LOCK_BATCH_SIZE]
        await db.execute(
            update(User)
            .where(User.id.in_(chunk))
            .values(status=_reactivated_status())
            .execution_options(synchronize_session=False)
        )
        rows = await db.execute(select(User.id, User.status).where(User.id.in_(chunk)).with_for_update())
        for user_id, status in rows.all():
            _set_unmodified(by_id[user_id], {"status": status})


def auto_delete_due(last_status_change: datetime | None, days: int | None, now: datetime) -> bool:
    if last_status_change is None or days is None or days < 0:
        return False
    if last_status_change.tzinfo is None:
        last_status_change = last_status_change.replace(tzinfo=UTC)
    return (now - last_status_change).days >= days
