from collections.abc import Iterable

from app.db.models import Admin, AdminStatus
from app.models.admin import AdminDetails, AdminModify
from app.models.admin_role import RoleLimits

API_KEY_IDENTITY_CHANGE_DENIED = (
    "A password or Telegram ID can be changed only from an admin session, not with an API key."
)


def _set_limits(limits: RoleLimits | None) -> dict:
    return limits.model_dump(exclude_none=True) if limits is not None else {}


def _stored_limits(db_admin: Admin) -> RoleLimits | None:
    if db_admin.permission_overrides is None:
        return None
    return RoleLimits.model_validate(db_admin.permission_overrides)


def _changes_login_identity(db_admin: Admin, modified_admin: AdminModify) -> bool:
    if modified_admin.password is not None:
        return True
    return modified_admin.telegram_id is not None and modified_admin.telegram_id != db_admin.telegram_id


def self_edit_refusal(db_admin: Admin, modified_admin: AdminModify, current_admin: AdminDetails) -> str | None:
    if current_admin.api_key_id is not None and _changes_login_identity(db_admin, modified_admin):
        return API_KEY_IDENTITY_CHANGE_DENIED
    if current_admin.is_owner or current_admin.id is None or db_admin.id != current_admin.id:
        return None
    if modified_admin.role_id is not None and modified_admin.role_id != db_admin.role_id:
        return "You're not allowed to change your own role."
    new_overrides = modified_admin.permission_overrides
    if new_overrides is not None and _set_limits(new_overrides) != _set_limits(_stored_limits(db_admin)):
        return "You're not allowed to change your own limits."
    if modified_admin.data_limit is not None and (modified_admin.data_limit or 0) != (db_admin.data_limit or 0):
        return "You're not allowed to change your own data limit."
    if modified_admin.status is not None and modified_admin.status != db_admin.status:
        return "You're not allowed to change your own status."
    return None


def _is_refusable_self(current_admin: AdminDetails) -> bool:
    return not current_admin.is_owner and current_admin.id is not None


def self_reset_refusal(target_ids: Iterable[int], current_admin: AdminDetails) -> str | None:
    if _is_refusable_self(current_admin) and current_admin.id in set(target_ids):
        return "You're not allowed to reset your own usage."
    return None


def self_enable_refusal(db_admins: Iterable[Admin], current_admin: AdminDetails) -> str | None:
    if not _is_refusable_self(current_admin):
        return None
    if any(db_admin.id == current_admin.id and db_admin.status != AdminStatus.active for db_admin in db_admins):
        return "You're not allowed to enable your own account."
    return None
