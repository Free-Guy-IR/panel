from app.db.models import Admin
from app.models.admin import AdminDetails, AdminModify
from app.models.admin_role import RoleLimits


def _set_limits(limits: RoleLimits | None) -> dict:
    return limits.model_dump(exclude_none=True) if limits is not None else {}


def _stored_limits(db_admin: Admin) -> RoleLimits | None:
    if db_admin.permission_overrides is None:
        return None
    return RoleLimits.model_validate(db_admin.permission_overrides)


def self_edit_refusal(db_admin: Admin, modified_admin: AdminModify, current_admin: AdminDetails) -> str | None:
    if current_admin.is_owner or current_admin.id is None or db_admin.id != current_admin.id:
        return None
    if modified_admin.role_id is not None and modified_admin.role_id != db_admin.role_id:
        return "You're not allowed to change your own role."
    new_overrides = modified_admin.permission_overrides
    if new_overrides is not None and _set_limits(new_overrides) != _set_limits(_stored_limits(db_admin)):
        return "You're not allowed to change your own limits."
    if modified_admin.data_limit is not None and (modified_admin.data_limit or 0) != (db_admin.data_limit or 0):
        return "You're not allowed to change your own data limit."
    return None
