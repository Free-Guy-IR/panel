from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession


class UsageWindow:
    __slots__ = ("reset_log_watermark", "reset_user_ids")

    def __init__(self) -> None:
        self.reset_log_watermark: int = 0
        self.reset_user_ids: set[int] = set()


class ResetHold:
    __slots__ = ("_barrier", "_released", "_user_ids")

    def __init__(self, barrier: UsageApplyBarrier, user_ids: frozenset[int]) -> None:
        self._barrier = barrier
        self._user_ids = user_ids
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._barrier.drop_hold(self._user_ids)


class UsageApplyBarrier:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._windows: list[UsageWindow] = []
        self._holds: Counter[int] = Counter()

    @property
    def open_windows(self) -> int:
        return len(self._windows)

    @property
    def held_user_ids(self) -> frozenset[int]:
        return frozenset(self._holds)

    def drop_hold(self, user_ids: frozenset[int]) -> None:
        for user_id in user_ids:
            remaining = self._holds.get(user_id, 0) - 1
            if remaining > 0:
                self._holds[user_id] = remaining
            else:
                self._holds.pop(user_id, None)

    @asynccontextmanager
    async def window(self) -> AsyncIterator[UsageWindow]:
        window = UsageWindow()
        async with self._lock:
            window.reset_user_ids.update(self._holds)
            self._windows.append(window)
        try:
            yield window
        finally:
            async with self._lock:
                if window in self._windows:
                    self._windows.remove(window)

    @asynccontextmanager
    async def apply(self, window: UsageWindow) -> AsyncIterator[frozenset[int]]:
        async with self._lock:
            yield frozenset(window.reset_user_ids)

    @asynccontextmanager
    async def reset(self, db: AsyncSession | None, user_ids: Iterable[int | None]) -> AsyncIterator[None]:
        marked = frozenset(int(user_id) for user_id in user_ids if user_id is not None)
        if not marked:
            yield
            return

        async with self._lock:
            for window in self._windows:
                window.reset_user_ids |= marked
            self._holds.update(marked)
            hold = ResetHold(self, marked)
            if db is not None:
                _release_when_transaction_ends(db, hold)

        try:
            yield
        finally:
            if db is None or not db.in_transaction():
                hold.release()


def _release_when_transaction_ends(db: AsyncSession, hold: ResetHold) -> None:
    sync_session = db.sync_session

    def _on_transaction_end(session, transaction) -> None:
        if hold.released or session.in_transaction():
            return
        hold.release()

    event.listen(sync_session, "after_transaction_end", _on_transaction_end)


usage_apply_barrier = UsageApplyBarrier()
