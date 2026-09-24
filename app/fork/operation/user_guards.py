from app.models.admin import AdminDetails
from app.operation.permissions import PermissionDenied, enforce_permission, get_scope_admin_id
from app.utils.system import readable_duration, readable_size


async def reject_negative_next_plan(op, db, next_plan) -> None:
    if next_plan.data_limit is not None and next_plan.data_limit < 0:
        await op.raise_error(message="Next plan data limit cannot be negative", code=400, db=db)
    if next_plan.expire is not None and next_plan.expire < 0:
        await op.raise_error(message="Next plan expire cannot be negative", code=400, db=db)


async def enforce_next_plan_limits(op, db, limits, next_plan) -> None:
    data_limit = next_plan.data_limit or 0
    if limits.data_limit_max is not None and data_limit <= 0:
        await op.raise_error(
            message=f"Next plan data limit cannot be unlimited; maximum is {readable_size(limits.data_limit_max)}",
            code=400,
            db=db,
        )
    if data_limit > 0:
        if limits.data_limit_min is not None and data_limit < limits.data_limit_min:
            await op.raise_error(
                message=f"Next plan data limit must be at least {readable_size(limits.data_limit_min)}",
                code=400,
                db=db,
            )
        if limits.data_limit_max is not None and data_limit > limits.data_limit_max:
            await op.raise_error(
                message=f"Next plan data limit cannot exceed {readable_size(limits.data_limit_max)}", code=400, db=db
            )
    if limits.data_limit_max is not None and next_plan.add_remaining_traffic:
        await op.raise_error(
            message="Next plan cannot add the remaining traffic while your role caps the data limit", code=400, db=db
        )

    expire = next_plan.expire or 0
    if limits.expire_max is not None and expire <= 0:
        await op.raise_error(
            message=f"Next plan expire cannot be unlimited; maximum is {readable_duration(limits.expire_max)}",
            code=400,
            db=db,
        )
    if expire > 0:
        if limits.expire_min is not None and expire < limits.expire_min:
            await op.raise_error(
                message=f"Next plan expire must be at least {readable_duration(limits.expire_min)}", code=400, db=db
            )
        if limits.expire_max is not None and expire > limits.expire_max:
            await op.raise_error(
                message=f"Next plan expire cannot exceed {readable_duration(limits.expire_max)}", code=400, db=db
            )


def _may_update_admins(admin: AdminDetails) -> bool:
    try:
        enforce_permission(admin, "admins", "update")
    except PermissionDenied:
        return False
    return True


async def ensure_owner_target_allowed(op, new_admin, admin: AdminDetails) -> None:
    if admin.is_owner:
        return
    if new_admin.role_id == 1 or (new_admin.role is not None and new_admin.role.is_owner):
        await op.raise_error(message="Owner account is not accessible.", code=403)
    if (
        get_scope_admin_id(admin, "users", "set_owner") is not None
        and new_admin.id != admin.id
        and not _may_update_admins(admin)
    ):
        await op.raise_error(message="You're not allowed to move users to another admin", code=403)
