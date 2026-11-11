from sqlalchemy import select
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from app.db import AsyncSession
from app.db.models import ProxyHost
from app.models.admin import AdminDetails
from app.models.host import CreateHost
from app.operation.permissions import PermissionDenied, enforce_permission


def creation_refusal(admin: AdminDetails, hosts: list[CreateHost], existing: dict[int, ProxyHost]) -> str | None:
    if all(existing.get(host.id) is not None for host in hosts):
        return None
    try:
        enforce_permission(admin, "hosts", "create")
    except PermissionDenied as exc:
        return str(exc)
    return None


async def vanished_hosts_conflict(
    db: AsyncSession, error: Exception, hosts: list[CreateHost], existing: dict[int, ProxyHost]
) -> str | None:
    if not isinstance(error, StaleDataError | ObjectDeletedError):
        return None
    edited = {host.id for host in hosts if existing.get(host.id) is not None}
    if not edited:
        return None
    remaining = set((await db.execute(select(ProxyHost.id).where(ProxyHost.id.in_(edited)))).scalars())
    vanished = sorted(edited - remaining)
    if not vanished:
        return None
    return f"Host {', '.join(str(host_id) for host_id in vanished)} was deleted while it was being edited"
