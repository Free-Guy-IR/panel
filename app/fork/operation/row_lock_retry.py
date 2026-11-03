import asyncio
import random

from sqlalchemy import event
from sqlalchemy.exc import DBAPIError

from app.db import AsyncSession
from app.fork.proxy_secrets.locks import is_row_lock_conflict

ROW_LOCK_ATTEMPTS = 3
ROW_LOCK_JITTER_MIN = 0.05
ROW_LOCK_JITTER_MAX = 0.25
ADMIN_REMOVAL_LOCK_UNAVAILABLE_DETAIL = (
    "another request held some of the admin's users and the removal could not lock them in time; "
    "nothing was removed, retry shortly"
)


async def retry_on_row_lock_conflict(operation, db: AsyncSession, attempt_once, detail: str):
    commits: list[object] = []

    def record_commit(session) -> None:
        commits.append(session)

    event.listen(db.sync_session, "after_commit", record_commit)
    try:
        for attempt in range(1, ROW_LOCK_ATTEMPTS + 1):
            try:
                return await attempt_once()
            except DBAPIError as exc:
                if commits or not is_row_lock_conflict(exc, db.get_bind().dialect):
                    raise
                await db.rollback()
                if attempt == ROW_LOCK_ATTEMPTS:
                    await operation.raise_error(message=detail, code=503, db=db)
                await asyncio.sleep(random.uniform(ROW_LOCK_JITTER_MIN, ROW_LOCK_JITTER_MAX) * attempt)
    finally:
        event.remove(db.sync_session, "after_commit", record_commit)
