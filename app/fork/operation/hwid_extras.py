from sqlalchemy import delete as sa_delete

from app.db import AsyncSession
from app.db.models import UserConnectionState


async def clear_connection_state(db: AsyncSession, user_id: int) -> None:
    await db.execute(sa_delete(UserConnectionState).where(UserConnectionState.user_id == user_id))
    await db.commit()
