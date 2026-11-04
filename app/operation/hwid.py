from app.db import AsyncSession
from app.db.crud.hwid import delete_user_hwid, get_user_hwids, reset_user_hwids
from app.fork.operation.hwid_extras import clear_connection_state
from app.models.admin import AdminDetails
from app.models.user import UserHWIDListResponse, UserHWIDResponse
from app.operation import BaseOperation
from app.operation.permissions import PermissionDenied, enforce_permission

_clear_connection_state = clear_connection_state



class HWIDOperation(BaseOperation):
    async def _get_hwid_user(
        self, db: AsyncSession, user_id: int, admin: AdminDetails, hwid_action: str, *user_scope_actions: str
    ):
        try:
            enforce_permission(admin, "users", "read")
        except PermissionDenied:
            await self.raise_error(message="User not found", code=404)
        for scope_action in user_scope_actions:
            await self.get_validated_user_by_id(db, user_id, admin, scope_action=scope_action)
        return await self.get_validated_user_by_id(db, user_id, admin, scope_resource="hwids", scope_action=hwid_action)

    async def get_user_hwids(self, db: AsyncSession, user_id: int, admin: AdminDetails) -> UserHWIDListResponse:
        db_user = await self._get_hwid_user(db, user_id, admin, "read", "read")
        hwids = await get_user_hwids(db, db_user.id)
        hwid_responses = [UserHWIDResponse.model_validate(h) for h in hwids]
        return UserHWIDListResponse(hwids=hwid_responses, count=len(hwid_responses))

    async def delete_user_hwid(self, db: AsyncSession, user_id: int, hwid: str, admin: AdminDetails) -> dict:
        db_user = await self._get_hwid_user(db, user_id, admin, "delete", "read", "delete")
        deleted = await delete_user_hwid(db, db_user.id, hwid)
        if not deleted:
            await self.raise_error(message="HWID not found", code=404)
        await _clear_connection_state(db, db_user.id)
        return {}

    async def reset_user_hwids(self, db: AsyncSession, user_id: int, admin: AdminDetails) -> dict:
        db_user = await self._get_hwid_user(db, user_id, admin, "delete", "read", "delete")
        count = await reset_user_hwids(db, db_user.id)
        await _clear_connection_state(db, db_user.id)
        return {"count": count}
