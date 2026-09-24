import asyncio
import weakref

from app.db.models import AdminStatus

_admin_transition_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def admin_transition_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _admin_transition_locks.get(loop)
    if lock is None:
        lock = _admin_transition_locks[loop] = asyncio.Lock()
    return lock


def status_blocks_user_sync(status: AdminStatus | None, role) -> bool:
    if status == AdminStatus.limited:
        return bool(role and role.disconnect_users_when_limited)
    if status == AdminStatus.disabled:
        return bool(role and role.disconnect_users_when_disabled)
    return False
