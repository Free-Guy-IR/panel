from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from sqlalchemy import select

from app.db import AsyncSession
from app.db.models import User
from app.fork.proxy_secrets.entitled import issue_unique_secrets, put_entitled_secret


async def fill_missing_sections(db: AsyncSession, spec, user_ids: Sequence[int], generate: Callable[[], str]) -> int:
    rows = await db.execute(select(User.id, User.proxy_settings).where(User.id.in_(user_ids)).order_by(User.id))
    missing = [user_id for user_id, settings in rows.all() if spec.protocol not in (settings or {})]
    if not missing:
        return 0
    values = await issue_unique_secrets(db, replace(spec, generate=generate), len(missing))
    filled = 0
    for user_id, value in zip(missing, values, strict=True):
        if await put_entitled_secret(db, spec, user_id, None, value):
            filled += 1
    return filled
