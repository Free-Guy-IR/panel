from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.admin import build_admin_details
from app.db.models import Admin
from app.fork.crud.admin_scope import count_visible_admin_users, visible_admin_user_ids
from app.fork.operation.base_extras import restrict_users_query_by_groups
from app.models.admin import AdminDetails
from app.models.user import UserListQuery
from app.operation.permissions import get_allowed_group_ids
from app.utils.connection_enforcement import close_active_restrictions, close_active_restrictions_for_admin


async def close_visible_restrictions(db: AsyncSession, admin_id: int, caller: AdminDetails, reason: str) -> int:
    allowed_group_ids = get_allowed_group_ids(caller)
    if allowed_group_ids is None:
        return await close_active_restrictions_for_admin(db, admin_id, reason)
    return await close_active_restrictions(db, await visible_admin_user_ids(db, admin_id, allowed_group_ids), reason)


def visible_users_query(caller: AdminDetails) -> UserListQuery | None:
    query, nothing_visible = restrict_users_query_by_groups(caller, UserListQuery())
    return None if nothing_visible else query


async def admin_details_for(db: AsyncSession, db_admin: Admin, caller: AdminDetails) -> AdminDetails:
    allowed_group_ids = get_allowed_group_ids(caller)
    if allowed_group_ids is None:
        return build_admin_details(db_admin, include_loaded_metrics=True)
    total_users = await count_visible_admin_users(db, db_admin.id, allowed_group_ids)
    return build_admin_details(db_admin, include_loaded_metrics=True, total_users=total_users)
