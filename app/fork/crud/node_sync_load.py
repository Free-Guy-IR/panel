from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession


@asynccontextmanager
async def committed_state_session(db: AsyncSession) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(bind=db.bind, expire_on_commit=False) as read_db:
        yield read_db
