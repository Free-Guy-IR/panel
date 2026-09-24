from datetime import UTC, datetime as dt

from sqlalchemy import and_, case, cast, delete, func, literal, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.db.models import (
    Admin,
    Group,
    NextPlan,
    NodeUserUsage,
    User,
    UserStatus,
    UserUsageResetLogs,
    users_groups_association,
)
from app.fork.crud.group_scope import user_in_allowed_groups
from app.models.group import BulkGroup
from app.models.user import BulkUser, BulkUserFilter, BulkUsersProxy

from .general import get_datetime_add_expression
from .user import _build_user_select_stmt


def _bulk_user_reload_stmt() -> Select:
    """Reload bulk results with batched relationships and aggregate lifetime usage."""
    # Bulk SQL bypasses relationship collections; replace any cached memberships.
    return _build_user_select_stmt(
        load_admin_role=True,
        load_usage_logs=False,
        load_lifetime_used_traffic=True,
    ).execution_options(populate_existing=True)


async def reset_all_users_data_usage(
    db: AsyncSession,
    admin: Admin | None = None,
    *,
    clean_chart_data: bool = False,
    allowed_group_ids: list[int] | None = None,
):
    """
    Efficiently resets data usage for all users, or users under a specific admin if provided.

    This function performs a high-performance reset by executing bulk database operations
    rather than iterating over ORM-mapped objects, improving speed and reducing memory usage.

    Operations performed:
        - Sets `used_traffic` to 0 for all target users.
        - Sets `status` to `active` for all users, unless filtered by admin.
        - Deletes all related `UserUsageResetLogs` and `NextPlan` entries.
        - Deletes `NodeUserUsage` chart rows when `clean_chart_data` is enabled.

    Args:
        db (AsyncSession): The SQLAlchemy async session used for database operations.
        admin (Optional[Admin]): If provided, only resets data usage for users belonging to this admin.
                                 If None, resets usage for all users in the system.

    Notes:
        - All operations are executed in bulk for performance.
        - This function assumes proper foreign key constraints and cascading rules are in place.
        - The function commits changes at the end of the operation.
    """
    user_ids_query = select(User.id).where(User.admin_id == admin.id) if admin else select(User.id)
    if allowed_group_ids is not None:
        user_ids_query = user_ids_query.where(user_in_allowed_groups(User.id, allowed_group_ids))
    user_ids = (await db.execute(user_ids_query)).scalars().all()

    if not user_ids:
        return

    reset_status = case((User.status == UserStatus.limited, UserStatus.active), else_=User.status)

    await db.execute(
        update(User)
        .where(User.id.in_(user_ids))
        .values(used_traffic=0, usage_epoch=User.usage_epoch + 1, status=reset_status)
    )

    await db.execute(delete(UserUsageResetLogs).where(UserUsageResetLogs.user_id.in_(user_ids)))
    if clean_chart_data:
        await db.execute(delete(NodeUserUsage).where(NodeUserUsage.user_id.in_(user_ids)))
    await db.execute(delete(NextPlan).where(NextPlan.user_id.in_(user_ids)))

    await db.commit()


async def disable_all_active_users(
    db: AsyncSession, admin: Admin | None = None, *, allowed_group_ids: list[int] | None = None
):
    """
    Disable all active users or users under a specific admin.

    Args:
        db (AsyncSession): Database session.
        admin (Optional[Admin]): Admin to filter users by, if any.
    """
    query = update(User).where(User.status.in_((UserStatus.active, UserStatus.on_hold)))
    if admin:
        query = query.filter(User.admin_id == admin.id)
    if allowed_group_ids is not None:
        query = query.filter(user_in_allowed_groups(User.id, allowed_group_ids))

    await db.execute(
        query.values(
            {User.status: UserStatus.disabled, User.last_status_change: dt.now(UTC)},
        )
    )

    await db.commit()
    await db.refresh(admin)


async def activate_all_disabled_users(
    db: AsyncSession, admin: Admin | None = None, *, allowed_group_ids: list[int] | None = None
):
    """
    Activate all disabled users or users under a specific admin.

    Users whose expire date has passed become expired and users over their
    data limit become limited instead of being revived to active.

    Args:
        db (AsyncSession): Database session.
        admin (Optional[Admin]): Admin to filter users by, if any.
    """
    filters = [User.status == UserStatus.disabled]
    if admin:
        filters.append(User.admin_id == admin.id)
    if allowed_group_ids is not None:
        filters.append(user_in_allowed_groups(User.id, allowed_group_ids))

    status_type = User.__table__.c.status.type
    status_case = case(
        (User.is_expired, literal(UserStatus.expired, status_type)),
        (User.is_limited, literal(UserStatus.limited, status_type)),
        (
            and_(User.expire.is_(None), User.on_hold_expire_duration.isnot(None)),
            literal(UserStatus.on_hold, status_type),
        ),
        else_=literal(UserStatus.active, status_type),
    )

    await db.execute(
        update(User)
        .where(and_(*filters))
        .values(
            {User.status: status_case, User.last_status_change: dt.now(UTC)},
        )
    )

    await db.commit()
    await db.refresh(admin)


def _create_group_filter(bulk_model: BulkGroup, allowed_group_ids: list[int] | None = None):
    """Create a comprehensive SQLAlchemy filter condition from a BulkGroup model."""
    other_conditions = []
    if bulk_model.admins:
        other_conditions.append(User.admin_id.in_(bulk_model.admins))
    if bulk_model.has_group_ids:
        other_conditions.append(User.groups.any(Group.id.in_(bulk_model.has_group_ids)))
    if bulk_model.has_no_group:
        other_conditions.append(~User.groups.any())

    user_ids = bulk_model.users or []

    filter_conditions = []
    if user_ids:
        filter_conditions.append(User.id.in_(user_ids))
    if other_conditions:
        filter_conditions.append(and_(*other_conditions))

    if len(filter_conditions) > 1:
        target = or_(*filter_conditions)
    elif filter_conditions:
        target = filter_conditions[0]
    else:
        target = True
    if allowed_group_ids is None:
        return target
    return and_(target, user_in_allowed_groups(User.id, allowed_group_ids))


async def add_groups_to_users(
    db: AsyncSession, bulk_model: BulkGroup, allowed_group_ids: list[int] | None = None
) -> tuple[list, int] | tuple[list[User], int]:
    """
    Bulk add groups to users and return list of affected User objects.
    """
    final_filter = _create_group_filter(bulk_model, allowed_group_ids)

    # Get target user IDs
    result = await db.execute(select(User.id).where(final_filter))
    user_ids = {row[0] for row in result.all()}

    count_effctive_users = len(user_ids)

    if not user_ids:
        return [], count_effctive_users

    # Fetch existing associations for target users
    existing = await db.execute(
        select(users_groups_association).where(users_groups_association.c.user_id.in_(user_ids))
    )
    existing_pairs = {(r.user_id, r.groups_id) for r in existing.all()}

    # Prepare new associations
    new_rows = [
        {"user_id": uid, "groups_id": gid}
        for uid in user_ids
        for gid in bulk_model.group_ids
        if (uid, gid) not in existing_pairs
    ]

    if not new_rows:
        return [], count_effctive_users

    # PostgreSQL asyncpg limits bind parameters to 32767 per query.
    # Each row has 2 columns (user_id, groups_id), so cap batches at 16000 rows.
    BATCH_SIZE = 16_000
    for i in range(0, len(new_rows), BATCH_SIZE):
        await db.execute(users_groups_association.insert(), new_rows[i : i + BATCH_SIZE])
    await db.commit()

    # Return users that actually had groups added
    result = await db.execute(_bulk_user_reload_stmt().where(User.id.in_({r["user_id"] for r in new_rows})))
    users = result.scalars().all()
    return users, count_effctive_users


async def remove_groups_from_users(
    db: AsyncSession, bulk_model: BulkGroup, allowed_group_ids: list[int] | None = None
) -> tuple[list, int] | tuple[list[User], int]:
    """
    Bulk remove groups from users and return list of affected User objects.
    """
    final_filter = _create_group_filter(bulk_model, allowed_group_ids)

    # Get target user IDs
    result = await db.execute(select(User.id).where(final_filter))
    user_ids = {row[0] for row in result.all()}

    count_effctive_users = len(user_ids)

    if not user_ids:
        return [], count_effctive_users

    # Identify affected users (those who actually have the groups to be removed)
    subquery = (
        select(users_groups_association.c.user_id)
        .where(
            and_(
                users_groups_association.c.user_id.in_(user_ids),
                users_groups_association.c.groups_id.in_(bulk_model.group_ids),
            )
        )
        .distinct()
    )
    result = await db.execute(select(User.id).where(User.id.in_(subquery)))
    affected_user_ids = result.scalars().all()

    if not affected_user_ids:
        return [], count_effctive_users

    await db.execute(
        delete(users_groups_association).where(
            users_groups_association.c.user_id.in_(affected_user_ids),
            users_groups_association.c.groups_id.in_(bulk_model.group_ids),
        )
    )
    await db.commit()
    result = await db.execute(_bulk_user_reload_stmt().where(User.id.in_(affected_user_ids)))
    users = result.scalars().all()
    return users, count_effctive_users


async def count_bulk_expire_targets(
    db: AsyncSession, bulk_model: BulkUser, allowed_group_ids: list[int] | None = None
) -> int:
    final_filter = _create_final_filter(bulk_model, allowed_group_ids)
    return (
        await db.execute(select(func.count(User.id)).where(and_(final_filter, User.expire.isnot(None))))
    ).scalar_one_or_none() or 0


async def count_bulk_datalimit_targets(
    db: AsyncSession, bulk_model: BulkUser, allowed_group_ids: list[int] | None = None
) -> int:
    final_filter = _create_final_filter(bulk_model, allowed_group_ids)
    return (
        await db.execute(
            select(func.count(User.id)).where(and_(final_filter, User.data_limit.isnot(None), User.data_limit != 0))
        )
    ).scalar_one_or_none() or 0


async def count_bulk_proxy_targets(
    db: AsyncSession, bulk_model: BulkUsersProxy, allowed_group_ids: list[int] | None = None
) -> int:
    final_filter = _create_final_filter(bulk_model, allowed_group_ids)
    return (await db.execute(select(func.count(User.id)).where(final_filter))).scalar_one_or_none() or 0


async def count_bulk_group_scope(
    db: AsyncSession, bulk_model: BulkGroup, allowed_group_ids: list[int] | None = None
) -> int:
    final_filter = _create_group_filter(bulk_model, allowed_group_ids)
    return (await db.execute(select(func.count(User.id)).where(final_filter))).scalar_one_or_none() or 0


def _create_final_filter(bulk_model: BulkUserFilter, allowed_group_ids: list[int] | None = None):
    """Create a comprehensive SQLAlchemy filter condition from a bulk model."""
    restrictions = []
    if bulk_model.status:
        restrictions.append(User.status.in_([i.value for i in bulk_model.status]))
    if bulk_model.expire_after:
        restrictions.append(User.expire >= bulk_model.expire_after)
    if bulk_model.expire_before:
        restrictions.append(User.expire <= bulk_model.expire_before)

    membership = []
    if bulk_model.admins:
        membership.append(User.admin_id.in_([i for i in bulk_model.admins]))
    if bulk_model.group_ids:
        membership.append(User.groups.any(Group.id.in_(bulk_model.group_ids)))

    targets = []
    if membership:
        targets.append(and_(*membership))
    if bulk_model.users:
        targets.append(User.id.in_(bulk_model.users))

    conditions = list(restrictions)
    if targets:
        conditions.append(or_(*targets))
    if allowed_group_ids is not None:
        conditions.append(user_in_allowed_groups(User.id, allowed_group_ids))

    if not conditions:
        return True
    return and_(*conditions)


BULK_STATUS_CHUNK_SIZE = 5000


def _id_chunks(user_ids: list[int]) -> list[list[int]]:
    return [user_ids[i : i + BULK_STATUS_CHUNK_SIZE] for i in range(0, len(user_ids), BULK_STATUS_CHUNK_SIZE)]


async def _lock_bulk_targets(db: AsyncSession, condition) -> dict[int, UserStatus]:
    rows = await db.execute(select(User.id, User.status).where(condition).order_by(User.id).with_for_update())
    return {user_id: status for user_id, status in rows.all()}


async def _apply_to_locked_targets(
    db: AsyncSession, locked: dict[int, UserStatus], conditions: tuple, **values
) -> list[User]:
    changed_ids: list[int] = []
    for chunk in _id_chunks(sorted(locked)):
        await db.execute(update(User).where(User.id.in_(chunk), *conditions).values(**values))
        rows = await db.execute(select(User.id, User.status).where(User.id.in_(chunk)).with_for_update())
        changed_ids.extend(user_id for user_id, status in rows.all() if status != locked[user_id])
    await db.commit()

    users: list[User] = []
    for chunk in _id_chunks(changed_ids):
        users.extend((await db.execute(_bulk_user_reload_stmt().where(User.id.in_(chunk)))).scalars().all())
    return users


async def update_users_expire(
    db: AsyncSession, bulk_model: BulkUser, allowed_group_ids: list[int] | None = None
) -> tuple[list[User], int] | tuple[list, int]:
    """
    Bulk update user expiration dates and return list of User objects where status changed.
    """
    target_conditions = (User.expire.isnot(None),)
    locked = await _lock_bulk_targets(db, and_(_create_final_filter(bulk_model, allowed_group_ids), *target_conditions))
    if not locked:
        await db.commit()
        return [], 0

    new_expire = get_datetime_add_expression(db, User.expire, bulk_model.amount)
    current_time = dt.now(UTC)
    status_cases = [
        (and_(new_expire <= current_time, User.status == UserStatus.active), UserStatus.expired),
        (and_(new_expire > current_time, User.status == UserStatus.expired), UserStatus.active),
    ]
    users = await _apply_to_locked_targets(
        db,
        locked,
        target_conditions,
        expire=new_expire,
        status=case(*status_cases, else_=User.status),
    )
    return users, len(locked)


async def update_users_datalimit(
    db: AsyncSession, bulk_model: BulkUser, allowed_group_ids: list[int] | None = None
) -> tuple[list[User], int] | tuple[list, int]:
    """
    Bulk update user data limits and return list of User objects where status changed.
    """
    target_conditions = [User.data_limit.isnot(None), User.data_limit != 0]
    if bulk_model.amount < 0:
        target_conditions.append(User.data_limit > bulk_model.amount * -1)
    locked = await _lock_bulk_targets(db, and_(_create_final_filter(bulk_model, allowed_group_ids), *target_conditions))
    if not locked:
        await db.commit()
        return [], 0

    status_cases = [
        (
            and_(User.data_limit + bulk_model.amount <= User.used_traffic, User.status == UserStatus.active),
            UserStatus.limited,
        ),
        (
            and_(User.data_limit + bulk_model.amount > User.used_traffic, User.status == UserStatus.limited),
            UserStatus.active,
        ),
    ]
    users = await _apply_to_locked_targets(
        db,
        locked,
        tuple(target_conditions),
        data_limit=User.data_limit + bulk_model.amount,
        status=case(*status_cases, else_=User.status),
    )
    return users, len(locked)


async def update_users_proxy_settings(
    db: AsyncSession, bulk_model: BulkUsersProxy, allowed_group_ids: list[int] | None = None
) -> tuple[list, int] | tuple[list[User], int]:
    """
    Bulk update the `proxy_settings` JSON field for users and return updated rows.
    """
    final_filter = _create_final_filter(bulk_model, allowed_group_ids)

    # Capture target IDs without materializing users before the update.
    select_stmt = select(User.id).where(final_filter)
    result = await db.execute(select_stmt)
    updated_user_ids = result.scalars().all()
    count_effctive_users = len(updated_user_ids)

    if not updated_user_ids:
        return [], count_effctive_users

    dialect = db.bind.dialect.name

    # Prepare the update statement
    if dialect == "postgresql":
        proxy_settings_expr = cast(User.proxy_settings, JSONB)
        if bulk_model.method is not None:
            proxy_settings_expr = func.jsonb_set(
                proxy_settings_expr,
                text("'{shadowsocks,method}'"),
                cast(f"{bulk_model.method.value}", JSONB),
                True,
            )
    else:
        proxy_settings_expr = User.proxy_settings
        if bulk_model.method is not None:
            proxy_settings_expr = func.json_set(
                proxy_settings_expr, "$.shadowsocks.method", f"{bulk_model.method.value}"
            )

    # Perform the update
    update_stmt = update(User).where(final_filter).values(proxy_settings=proxy_settings_expr)
    await db.execute(update_stmt)
    await db.commit()

    # Reload the captured targets and preserve their original selection order.
    result = await db.execute(_bulk_user_reload_stmt().where(User.id.in_(updated_user_ids)))
    refreshed_users = result.scalars().all()
    refreshed_users_map = {user.id: user for user in refreshed_users}
    ordered_refreshed_users = [refreshed_users_map[uid] for uid in updated_user_ids if uid in refreshed_users_map]

    return ordered_refreshed_users, count_effctive_users

from app.fork.crud.bulk import get_users_for_l2tp_activation, get_users_for_mtproto_activation  # noqa: F401
