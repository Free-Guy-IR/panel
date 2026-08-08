from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.crud.settings import get_settings
from app.db.models import User, UserConnectionLimit, UserConnectionState
from app.models.admin import AdminDetails
from app.models.connection_limit import (
    ConnectionStateResponse,
    ConnectionStatesResponse,
    ResolvedAddress,
    ResolvedAddressesResponse,
    UserConnectionLimitPayload,
    UserConnectionLimitResponse,
    UserConnectionLimitsResponse,
)
from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import DEFAULT_CDN_RANGES, lookup_providers

from .authentication import require_permission

router = APIRouter(tags=["Connection Limit"], prefix="/api/connection-limit")


async def _settings(db: AsyncSession) -> ConnectionLimit:
    """Stored settings, or the defaults when nothing has been saved yet."""
    row = await get_settings(db)
    stored = getattr(row, "connection_limit", None) if row is not None else None
    if not stored:
        return ConnectionLimit(cdn_ranges=DEFAULT_CDN_RANGES)
    settings = ConnectionLimit.model_validate(stored)
    if not settings.cdn_ranges:
        settings = settings.model_copy(update={"cdn_ranges": DEFAULT_CDN_RANGES})
    return settings


@router.get("/states", response_model=ConnectionStatesResponse)
async def list_connection_states(
    verdict: str | None = Query(default=None, description="Filter to one verdict"),
    min_devices: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(require_permission("settings", "read")),
):
    """Users ordered by how many devices were last seen on them."""
    settings = await _settings(db)

    stmt = select(UserConnectionState, User.username).join(User, User.id == UserConnectionState.user_id)
    if verdict:
        stmt = stmt.where(UserConnectionState.verdict == verdict)
    if min_devices is not None:
        stmt = stmt.where(UserConnectionState.devices >= min_devices)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))

    rows = (
        await db.execute(
            stmt.order_by(UserConnectionState.devices.desc(), UserConnectionState.streak.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    states = []
    for state, username in rows:
        item = ConnectionStateResponse.model_validate(state)
        item.username = username
        states.append(item)

    return ConnectionStatesResponse(
        states=states,
        total=total or 0,
        device_limit=settings.device_limit,
        enabled=settings.enabled,
        monitor_only=settings.monitor_only,
    )


@router.get("/states/by-user", response_model=ConnectionStatesResponse)
async def connection_states_for_users(
    user_ids: Annotated[list[int] | None, Query()] = None,
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(require_permission("users", "read")),
):
    """States for the users on one page of the users table.

    Taking the ids the page is already showing keeps this to a single indexed
    query, rather than a request per row.
    """
    settings = await _settings(db)
    if not user_ids:
        return ConnectionStatesResponse(
            states=[], total=0, device_limit=settings.device_limit,
            enabled=settings.enabled, monitor_only=settings.monitor_only,
        )

    rows = (
        await db.execute(
            select(UserConnectionState, User.username)
            .join(User, User.id == UserConnectionState.user_id)
            .where(UserConnectionState.user_id.in_(user_ids[:500]))
        )
    ).all()

    states = []
    for state, username in rows:
        item = ConnectionStateResponse.model_validate(state)
        item.username = username
        states.append(item)

    return ConnectionStatesResponse(
        states=states,
        total=len(states),
        device_limit=settings.device_limit,
        enabled=settings.enabled,
        monitor_only=settings.monitor_only,
    )


@router.get("/defaults/cdn-ranges", response_model=list[str])
async def default_cdn_ranges(_: AdminDetails = Depends(require_permission("settings", "read"))):
    """The CDN ranges shipped by default, so the settings form can offer them."""
    return DEFAULT_CDN_RANGES


@router.get("/overrides", response_model=UserConnectionLimitsResponse)
async def list_overrides(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(require_permission("settings", "read")),
):
    """Users given their own allowance, or exempted from checking."""
    settings = await _settings(db)

    stmt = select(UserConnectionLimit, User.username).join(User, User.id == UserConnectionLimit.user_id)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(User.username).limit(limit).offset(offset))).all()

    overrides = []
    for row, username in rows:
        item = UserConnectionLimitResponse.model_validate(row)
        item.username = username
        overrides.append(item)

    return UserConnectionLimitsResponse(
        overrides=overrides, total=total or 0, default_device_limit=settings.device_limit
    )


@router.put("/overrides/{user_id}", response_model=UserConnectionLimitResponse)
async def set_override(
    user_id: int,
    payload: UserConnectionLimitPayload,
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(require_permission("settings", "update")),
):
    """Give one user their own allowance, or exempt them."""
    user = (await db.execute(select(User).where(User.id == user_id))).scalar()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    row = (
        await db.execute(select(UserConnectionLimit).where(UserConnectionLimit.user_id == user_id))
    ).scalar()
    if row is None:
        row = UserConnectionLimit(user_id=user_id)
        db.add(row)

    row.ip_limit = payload.ip_limit
    row.exempt = payload.exempt
    row.note = payload.note
    await db.commit()
    await db.refresh(row)

    result = UserConnectionLimitResponse.model_validate(row)
    result.username = user.username
    return result


@router.delete("/overrides/{user_id}", status_code=204)
async def clear_override(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(require_permission("settings", "update")),
):
    """Put a user back on the default allowance."""
    row = (
        await db.execute(select(UserConnectionLimit).where(UserConnectionLimit.user_id == user_id))
    ).scalar()
    if row is not None:
        await db.delete(row)
        await db.commit()


@router.get("/addresses/{user_id}", response_model=ResolvedAddressesResponse)
async def resolve_user_addresses(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(require_permission("users", "read")),
):
    """Name the provider behind each address this user was last seen on.

    Resolved here rather than in the checking loop: the loop handles well over
    a thousand users a minute and would turn into thousands of lookups, while
    a review only needs the handful an admin is actually looking at.
    """
    settings = await _settings(db)

    state = (
        await db.execute(select(UserConnectionState).where(UserConnectionState.user_id == user_id))
    ).scalar()
    if state is None:
        return ResolvedAddressesResponse(addresses=[], enabled=settings.resolve_isp)

    details = state.details or {}
    seen: list[str] = []
    for key in ("real_groups", "cdn_addresses", "infrastructure_addresses", "earlier_groups"):
        for value in details.get(key) or []:
            if value not in seen:
                seen.append(value)

    if not settings.resolve_isp:
        # Still list what was seen; just without the provider names.
        return ResolvedAddressesResponse(
            addresses=[ResolvedAddress(address=a) for a in seen], enabled=False
        )

    resolved = await lookup_providers(seen)
    return ResolvedAddressesResponse(
        addresses=[
            ResolvedAddress(address=a, provider=resolved.get(a, {}).get("provider"), country=resolved.get(a, {}).get("country"))
            for a in seen
        ],
        enabled=True,
    )
