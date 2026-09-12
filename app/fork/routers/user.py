from fastapi import APIRouter, Depends

from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.user import BulkUserFilter
from app.operation import OperatorType
from app.operation.user import UserOperation
from app.routers.authentication import require_scope_all
from app.utils import responses

user_operator = UserOperation(operator_type=OperatorType.API)
router = APIRouter(tags=["User"], prefix="/api/user", responses={401: responses._401})

BULK_MTPROTO_ACTIVATE_DESCRIPTION = """Generate an MTProto secret for existing users who have MTProto access via
their current groups but don't have one yet - e.g. users created before
MTProto was enabled on their group.

- **user_ids**: Optional list of user IDs to modify
- **admins**: Optional list of admin IDs — their users will be targeted
- **group_ids**: Optional list of group IDs to filter users by their group membership
- **status**: Optional status to filter users (e.g., "expired", "active"), empty means no filtering
- Sending no filters at all targets every user.

Users who already have an MTProto secret are never touched, even if they
match the filter - this only ever fills in a *missing* secret, never
regenerates an existing one."""

BULK_L2TP_ACTIVATE_DESCRIPTION = """Issue an L2TP password to existing users who have L2TP access via
their current groups but don't have one yet - e.g. users created before
L2TP was enabled on their group.

- **user_ids**: Optional list of user IDs to modify
- **admins**: Optional list of admin IDs — their users will be targeted
- **group_ids**: Optional list of group IDs to filter users by their group membership
- **status**: Optional status to filter users (e.g., "expired", "active"), empty means no filtering
- Sending no filters at all targets every user.

Users who already have an L2TP password are never touched, even if they
match the filter - this only ever fills in a *missing* password, never
regenerates an existing one."""


@router.post(
    "s/bulk/mtproto_activate",
    summary="Retroactively generate MTProto secrets for existing users",
    description=BULK_MTPROTO_ACTIVATE_DESCRIPTION,
    response_description="Success confirmation",
)
async def bulk_activate_mtproto_secrets(
    bulk_model: BulkUserFilter,
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(require_scope_all("users", "update")),
):
    return await user_operator.bulk_activate_mtproto_secrets(db, bulk_model)


@router.post(
    "s/bulk/l2tp_activate",
    summary="Retroactively issue L2TP passwords to existing users",
    description=BULK_L2TP_ACTIVATE_DESCRIPTION,
    response_description="Success confirmation",
)
async def bulk_activate_l2tp_passwords(
    bulk_model: BulkUserFilter,
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(require_scope_all("users", "update")),
):
    return await user_operator.bulk_activate_l2tp_passwords(db, bulk_model)
