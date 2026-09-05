from sqlalchemy import delete as sa_delete

from app.db import AsyncSession
from app.db.crud.hwid import delete_user_hwid, get_user_hwids, reset_user_hwids
from app.db.models import UserConnectionState
from app.models.admin import AdminDetails
from app.models.user import UserHWIDListResponse, UserHWIDResponse
from app.operation import BaseOperation


async def _clear_connection_state(db: AsyncSession, user_id: int) -> None:
    """Drop the device-review row so it no longer shows the devices just cleared.

    The row rebuilds on the limiter's next cycle from current data; removing it
    here just keeps the review from showing stale hardware ids in the meantime.
    """
    await db.execute(sa_delete(UserConnectionState).where(UserConnectionState.user_id == user_id))
    await db.commit()


class HWIDOperation(BaseOperation):
    async def get_user_hwids(self, db: AsyncSession, user_id: int, admin: AdminDetails) -> UserHWIDListResponse:
        db_user = await self.get_validated_user_by_id(db, user_id, admin, scope_action="read")
        hwids = await get_user_hwids(db, db_user.id)
        hwid_responses = [UserHWIDResponse.model_validate(h) for h in hwids]
        return UserHWIDListResponse(hwids=hwid_responses, count=len(hwid_responses))

    async def delete_user_hwid(self, db: AsyncSession, user_id: int, hwid: str, admin: AdminDetails) -> dict:
        db_user = await self.get_validated_user_by_id(db, user_id, admin, scope_action="delete")
        deleted = await delete_user_hwid(db, db_user.id, hwid)
        if not deleted:
            await self.raise_error(message="HWID not found", code=404)
        await _clear_connection_state(db, db_user.id)
        return {}

    async def reset_user_hwids(self, db: AsyncSession, user_id: int, admin: AdminDetails) -> dict:
        db_user = await self.get_validated_user_by_id(db, user_id, admin, scope_action="delete")
        count = await reset_user_hwids(db, db_user.id)
        await _clear_connection_state(db, db_user.id)
        return {"count": count}
