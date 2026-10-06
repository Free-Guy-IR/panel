from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.admin import build_admin_details
from app.db.models import Admin
from app.fork.crud.admin_scope import count_visible_admin_users, lock_admin_users
from app.fork.operation.base_extras import restrict_users_query_by_groups
from app.fork.proxy_secrets.locks import LOCK_UNAVAILABLE_DETAIL, is_lock_unavailable
from app.models.admin import AdminDetails
from app.models.user import UserListQuery
from app.operation.permissions import get_allowed_group_ids
from app.utils.connection_enforcement import close_active_restrictions


async def close_visible_restrictions(db: AsyncSession, admin_id: int, caller: AdminDetails, reason: str) -> int:
    locked = await lock_admin_users(db, admin_id, get_allowed_group_ids(caller))
    return await close_active_restrictions(db, locked, reason)


async def answer_503_if_lock_unavailable(operation, db: AsyncSession, exc: Exception) -> None:
    if not is_lock_unavailable(exc):
        raise exc
    await operation.raise_error(message=LOCK_UNAVAILABLE_DETAIL, code=503, db=db)


def visible_users_query(caller: AdminDetails) -> UserListQuery | None:
    query, nothing_visible = restrict_users_query_by_groups(caller, UserListQuery())
    return None if nothing_visible else query


async def admin_details_for(db: AsyncSession, db_admin: Admin, caller: AdminDetails) -> AdminDetails:
    allowed_group_ids = get_allowed_group_ids(caller)
    if allowed_group_ids is None:
        return build_admin_details(db_admin, include_loaded_metrics=True)
    total_users = await count_visible_admin_users(db, db_admin.id, allowed_group_ids)
    return build_admin_details(db_admin, include_loaded_metrics=True, total_users=total_users)
