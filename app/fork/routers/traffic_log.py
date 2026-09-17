import asyncio
import json
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime as dt, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from app.db import AsyncSession, GetDB, get_db
from app.fork.models.traffic_log import TrafficLogRecord
from app.fork.traffic_log import service
from app.fork.traffic_log.schemas import (
    HistoryPage,
    PurgeRequest,
    PurgeResult,
    SettingsUpdate,
    Status,
    Summary,
    as_utc,
)
from app.models.admin import AdminDetails
from app.routers.authentication import oauth2_scheme, require_permission, require_permission_for_request
from app.utils import responses

router = APIRouter(
    tags=["Traffic Log"], prefix="/api/traffic-log", responses={401: responses._401, 403: responses._403}
)

QUEUE_POLL_SECONDS = 1.0
REVALIDATE_SECONDS = 30.0
REVOKED = {"control": "revoked"}
UNKNOWN_USER = {"control": "unknown_user"}
UNKNOWN_USER_ID = -1
OWNER_MESSAGE = "only an admin with full panel access can use the traffic log"
PURGE_EVERYTHING = timedelta(days=3650)
MAX_PURGE_AGE_HOURS = 87_600
MAX_TEXT_QUERY = 255


def require_owner():
    checked = require_permission("nodes", "logs")

    async def dependency(admin: AdminDetails = Depends(checked)) -> AdminDetails:
        if not service.is_sudo(admin):
            raise HTTPException(status_code=403, detail=OWNER_MESSAGE)
        return admin

    return dependency


OWNER_ONLY = require_owner()


def _json_default(value):
    if isinstance(value, dt):
        return as_utc(value).isoformat()
    return str(value)


def _encode(item) -> str:
    return json.dumps(item, default=_json_default)


@router.get("/live")
async def live_feed(
    request: Request,
    username: str | None = Query(default=None, max_length=MAX_TEXT_QUERY),
    node_id: int | None = Query(default=None),
    token: str | None = Depends(oauth2_scheme),
):
    user_id: int | None = None
    unknown = False
    async with GetDB() as db:
        admin = await require_permission_for_request(request, db, token, "nodes", "logs")
        if not service.is_sudo(admin):
            raise HTTPException(status_code=403, detail=OWNER_MESSAGE)
        admin_id = service.scope_admin_id(admin)
        wanted = (username or "").strip()
        if wanted:
            resolved = await service.resolve_username(db, wanted)
            if resolved is None or not service.visible_to(admin, resolved):
                unknown = True
                user_id = UNKNOWN_USER_ID
            else:
                user_id = resolved.user_id

    async def event_stream() -> AsyncGenerator[str]:
        from app.fork.traffic_log import collector

        checked_at = time.monotonic()
        async with collector.subscribe(user_id=user_id, node_id=node_id, admin_id=admin_id) as queue:
            if unknown:
                yield _encode(UNKNOWN_USER)
            while not await request.is_disconnected():
                if time.monotonic() - checked_at >= REVALIDATE_SECONDS:
                    if not await _still_authorised(request, token, admin_id):
                        yield _encode(REVOKED)
                        return
                    checked_at = time.monotonic()
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=QUEUE_POLL_SECONDS)
                except TimeoutError:
                    continue
                yield _encode(item)

    return EventSourceResponse(event_stream())


async def _still_authorised(request: Request, token: str | None, admin_id: int | None) -> bool:
    try:
        async with GetDB() as db:
            admin = await require_permission_for_request(request, db, token, "nodes", "logs")
            if not service.is_sudo(admin):
                return False
            return service.scope_admin_id(admin) == admin_id
    except HTTPException:
        return False
    except Exception:
        return False


@router.get("/history", response_model=HistoryPage)
async def get_history(
    start: Annotated[dt, Query()],
    end: Annotated[dt, Query()],
    username: str | None = Query(default=None, max_length=MAX_TEXT_QUERY),
    node_id: int | None = Query(default=None),
    inbound: str | None = Query(default=None, max_length=MAX_TEXT_QUERY),
    destination: str | None = Query(default=None, max_length=MAX_TEXT_QUERY),
    refused: bool | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_ONLY),
):
    return await service.history(
        db,
        admin,
        start=start,
        end=end,
        username=username,
        node_id=node_id,
        inbound=inbound,
        destination=destination,
        refused=refused,
        cursor=cursor,
        limit=limit,
    )


@router.get("/summary", response_model=Summary)
async def get_summary(
    start: Annotated[dt, Query()],
    end: Annotated[dt, Query()],
    username: str | None = Query(default=None, max_length=MAX_TEXT_QUERY),
    node_id: int | None = Query(default=None),
    inbound: str | None = Query(default=None, max_length=MAX_TEXT_QUERY),
    destination: str | None = Query(default=None, max_length=MAX_TEXT_QUERY),
    refused: bool | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_ONLY),
):
    return await service.summary(
        db,
        admin,
        start=start,
        end=end,
        username=username,
        node_id=node_id,
        inbound=inbound,
        destination=destination,
        refused=refused,
    )


@router.get("/status", response_model=Status)
async def get_status(_: AdminDetails = Depends(OWNER_ONLY)):
    return await service.status_payload()


@router.put("/settings", response_model=Status)
async def update_settings(
    body: SettingsUpdate,
    admin: AdminDetails = Depends(OWNER_ONLY),
):
    from app.fork.traffic_log import collector

    if body.retention_hours is not None:
        await collector.set_retention_hours(body.retention_hours)
    if body.enabled is not None:
        await collector.set_enabled(body.enabled)
    return await service.status_payload()


@router.post("/purge", response_model=PurgeResult)
async def purge_records(
    body: PurgeRequest | None = None,
    admin: AdminDetails = Depends(OWNER_ONLY),
):
    from app.fork.jobs.traffic_log_purge import purge_before, reclaim_storage, reconcile_buckets, session_engine
    from app.fork.traffic_log import collector

    older_than_hours = body.older_than_hours if body is not None else None
    wants_reclaim = body.reclaim if body is not None else False

    now = dt.now(UTC)
    if older_than_hours is None:
        cutoff = now + PURGE_EVERYTHING
    else:
        cutoff = now - timedelta(hours=min(older_than_hours, MAX_PURGE_AGE_HOURS))

    reclaimed = False
    freed_bytes: int | None = None
    since_ingest = collector.ingest_watermark()
    async with collector.suspend_flush(), GetDB() as db:
        collector.mark_rows_dirty()
        removed, incomplete = await purge_before(db, cutoff)
        remaining = int(await db.scalar(select(func.count()).select_from(TrafficLogRecord)) or 0)
        await reconcile_buckets(
            db,
            collector,
            None if older_than_hours is None else cutoff,
            keep_after=now if older_than_hours is None else None,
            since_ingest=since_ingest,
            keep_stored_rows=incomplete,
        )
        engine = session_engine(db)

    if wants_reclaim and not incomplete:
        reclaimed, freed_bytes = await reclaim_storage(engine)

    collector.record_manual_purge(removed, incomplete)
    return PurgeResult(
        removed=removed,
        incomplete=incomplete,
        remaining=int(remaining or 0),
        retention_hours=await collector.effective_retention_hours(),
        reclaimed=reclaimed,
        freed_bytes=freed_bytes,
    )
