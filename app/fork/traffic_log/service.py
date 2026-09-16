from dataclasses import dataclass
from datetime import UTC, datetime as dt, timedelta

from fastapi import HTTPException
from sqlalchemy import and_, case, distinct, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node, User
from app.fork.models.traffic_log import TrafficLogIdentity, TrafficLogRecord
from app.fork.traffic_log.schemas import (
    HistoryItem,
    HistoryPage,
    Status,
    Summary,
    TopDestination,
    TopUser,
    as_utc,
)
from app.models.admin import AdminDetails
from config import job_settings

RANGE_MESSAGE = "history is kept for {hours} hours; choose a start within that window"
ORDER_MESSAGE = "the end of the range must come after its start"
CURSOR_MESSAGE = "invalid cursor"
TOP_LIMIT = 8
LIKE_ESCAPE = "\\"
MAX_CURSOR_ID = 2**63 - 1


@dataclass(frozen=True, slots=True)
class ResolvedUser:
    user_id: int
    admin_id: int | None


@dataclass(frozen=True, slots=True)
class RecordFilter:
    start: dt
    end: dt
    user_id: int | None = None
    node_id: int | None = None
    inbound: str | None = None
    destination: str | None = None
    refused: bool | None = None
    admin_id: int | None = None


def is_sudo(admin: AdminDetails) -> bool:
    return bool(admin.is_owner)


def scope_admin_id(admin: AdminDetails) -> int | None:
    if is_sudo(admin):
        return None
    if admin.id is None:
        raise HTTPException(status_code=403, detail="this admin has no identity to scope traffic by")
    return admin.id


def visible_to(admin: AdminDetails, resolved: ResolvedUser) -> bool:
    admin_id = scope_admin_id(admin)
    return admin_id is None or resolved.admin_id == admin_id


def range_message(hours: int) -> str:
    return RANGE_MESSAGE.format(hours=hours)


async def validate_range(start: dt, end: dt) -> tuple[dt, dt]:
    from app.fork.traffic_log import collector

    retention_hours = await collector.effective_retention_hours()
    try:
        start, end = as_utc(start), as_utc(end)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(status_code=422, detail=range_message(retention_hours)) from None
    if end <= start:
        raise HTTPException(status_code=422, detail=ORDER_MESSAGE)
    if start < dt.now(UTC) - timedelta(hours=retention_hours):
        raise HTTPException(status_code=422, detail=range_message(retention_hours))
    return start, end


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _contains(text: str) -> str:
    escaped = (
        text.lower()
        .replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _parse_cursor(cursor: str) -> tuple[dt, int]:
    stamp, _, raw_id = cursor.rpartition("|")
    try:
        last_seen = as_utc(dt.fromisoformat(stamp))
        record_id = int(raw_id)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(status_code=422, detail=CURSOR_MESSAGE) from None
    if not 0 <= record_id <= MAX_CURSOR_ID:
        raise HTTPException(status_code=422, detail=CURSOR_MESSAGE)
    return last_seen, record_id


def _encode_cursor(last_seen: dt, record_id: int) -> str:
    return f"{as_utc(last_seen).isoformat()}|{record_id}"


async def resolve_username(db: AsyncSession, username: str) -> ResolvedUser | None:
    row = (await db.execute(select(User.id, User.admin_id).where(User.username == username))).first()
    if row is None:
        row = (
            await db.execute(
                select(TrafficLogIdentity.user_id, TrafficLogIdentity.admin_id)
                .where(TrafficLogIdentity.username == username)
                .order_by(TrafficLogIdentity.updated_at.desc())
                .limit(1)
            )
        ).first()
    if row is None:
        return None
    return ResolvedUser(user_id=int(row[0]), admin_id=row[1])


async def node_names(db: AsyncSession, node_ids: set[int]) -> dict[int, str]:
    if not node_ids:
        return {}
    rows = (await db.execute(select(Node.id, Node.name).where(Node.id.in_(sorted(node_ids))))).all()
    return {int(node_id): name for node_id, name in rows}


async def _build_filter(
    db: AsyncSession,
    admin: AdminDetails,
    *,
    start: dt,
    end: dt,
    username: str | None,
    node_id: int | None,
    inbound: str | None,
    destination: str | None,
    refused: bool | None,
) -> RecordFilter | None:
    start, end = await validate_range(start, end)
    admin_id = scope_admin_id(admin)
    user_id = None
    wanted = _clean(username)
    if wanted is not None:
        resolved = await resolve_username(db, wanted)
        if resolved is None or (admin_id is not None and resolved.admin_id != admin_id):
            return None
        user_id = resolved.user_id
    return RecordFilter(
        start=start,
        end=end,
        user_id=user_id,
        node_id=node_id,
        inbound=_clean(inbound),
        destination=_clean(destination),
        refused=refused,
        admin_id=admin_id,
    )


def _clauses(spec: RecordFilter) -> list:
    clauses = [TrafficLogRecord.last_seen >= spec.start, TrafficLogRecord.last_seen <= spec.end]
    if spec.user_id is not None:
        clauses.append(TrafficLogRecord.user_id == spec.user_id)
    if spec.node_id is not None:
        clauses.append(TrafficLogRecord.node_id == spec.node_id)
    if spec.inbound is not None:
        clauses.append(TrafficLogRecord.inbound_tag == spec.inbound)
    if spec.destination is not None:
        clauses.append(func.lower(TrafficLogRecord.host).like(_contains(spec.destination), escape=LIKE_ESCAPE))
    if spec.refused is not None:
        clauses.append(TrafficLogRecord.refused == spec.refused)
    if spec.admin_id is not None:
        clauses.append(_current_owner() == spec.admin_id)
    return clauses


def _current_owner():
    exists = select(User.id).where(User.id == TrafficLogRecord.user_id).exists()
    live = select(User.admin_id).where(User.id == TrafficLogRecord.user_id).scalar_subquery()
    return case((exists, live), else_=TrafficLogIdentity.admin_id)


def _scoped(stmt, spec: RecordFilter):
    if spec.admin_id is not None:
        stmt = stmt.outerjoin(TrafficLogIdentity, TrafficLogIdentity.user_id == TrafficLogRecord.user_id)
    return stmt.where(*_clauses(spec))


def _refused_hits():
    return func.coalesce(func.sum(case((TrafficLogRecord.refused.is_(True), TrafficLogRecord.hits), else_=0)), 0)


async def history(
    db: AsyncSession,
    admin: AdminDetails,
    *,
    start: dt,
    end: dt,
    username: str | None = None,
    node_id: int | None = None,
    inbound: str | None = None,
    destination: str | None = None,
    refused: bool | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> HistoryPage:
    spec = await _build_filter(
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
    if spec is None:
        return HistoryPage()

    stmt = (
        select(TrafficLogRecord, TrafficLogIdentity.username, TrafficLogIdentity.deleted)
        .outerjoin(TrafficLogIdentity, TrafficLogIdentity.user_id == TrafficLogRecord.user_id)
        .where(*_clauses(spec))
    )
    if cursor:
        cursor_seen, cursor_id = _parse_cursor(cursor)
        stmt = stmt.where(
            or_(
                TrafficLogRecord.last_seen < cursor_seen,
                and_(TrafficLogRecord.last_seen == cursor_seen, TrafficLogRecord.id < cursor_id),
            )
        )
    stmt = stmt.order_by(TrafficLogRecord.last_seen.desc(), TrafficLogRecord.id.desc()).limit(limit + 1)
    rows = (await db.execute(stmt)).all()

    page = rows[:limit]
    names = await node_names(db, {int(record.node_id) for record, _, _ in page})
    items = [
        HistoryItem(
            id=record.id,
            bucket_start=record.bucket_start,
            first_seen=record.first_seen,
            last_seen=record.last_seen,
            hits=record.hits,
            user_id=record.user_id,
            username=identity_name or record.user_label or None,
            user_deleted=bool(deleted),
            node_id=record.node_id,
            node=names.get(int(record.node_id)),
            inbound=record.inbound_tag,
            host=record.host,
            port=record.port,
            protocol=record.protocol,
            route=record.route,
            refused=bool(record.refused),
        )
        for record, identity_name, deleted in page
    ]
    next_cursor = _encode_cursor(items[-1].last_seen, items[-1].id) if len(rows) > limit else None
    return HistoryPage(items=items, next_cursor=next_cursor)


async def summary(
    db: AsyncSession,
    admin: AdminDetails,
    *,
    start: dt,
    end: dt,
    username: str | None = None,
    node_id: int | None = None,
    inbound: str | None = None,
    destination: str | None = None,
    refused: bool | None = None,
) -> Summary:
    spec = await _build_filter(
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
    if spec is None:
        return Summary()

    totals = (
        await db.execute(
            _scoped(
                select(
                    func.coalesce(func.sum(TrafficLogRecord.hits), 0),
                    func.count(distinct(TrafficLogRecord.host)),
                    func.count(distinct(TrafficLogRecord.user_id)),
                    _refused_hits(),
                ).select_from(TrafficLogRecord),
                spec,
            )
        )
    ).one()

    user_hits = func.sum(TrafficLogRecord.hits).label("hits")
    user_rows = (
        await db.execute(
            _scoped(select(TrafficLogRecord.user_id, user_hits).select_from(TrafficLogRecord), spec)
            .where(TrafficLogRecord.user_id.is_not(None))
            .group_by(TrafficLogRecord.user_id)
            .order_by(user_hits.desc(), TrafficLogRecord.user_id)
            .limit(TOP_LIMIT)
        )
    ).all()
    user_ids = {int(user_id) for user_id, _ in user_rows}
    usernames: dict[int, str] = {}
    if user_ids:
        identity_rows = (
            await db.execute(
                select(TrafficLogIdentity.user_id, TrafficLogIdentity.username).where(
                    TrafficLogIdentity.user_id.in_(sorted(user_ids))
                )
            )
        ).all()
        usernames = {int(user_id): name for user_id, name in identity_rows}

    host_hits = func.sum(TrafficLogRecord.hits).label("hits")
    host_rows = (
        await db.execute(
            _scoped(
                select(TrafficLogRecord.host, host_hits, _refused_hits().label("refused")).select_from(
                    TrafficLogRecord
                ),
                spec,
            )
            .group_by(TrafficLogRecord.host)
            .order_by(host_hits.desc(), TrafficLogRecord.host)
            .limit(TOP_LIMIT)
        )
    ).all()

    return Summary(
        connections=int(totals[0] or 0),
        destinations=int(totals[1] or 0),
        users=int(totals[2] or 0),
        refused=int(totals[3] or 0),
        top_users=[
            TopUser(user_id=int(user_id), username=usernames.get(int(user_id)), hits=int(hits or 0))
            for user_id, hits in user_rows
        ],
        top_destinations=[
            TopDestination(host=host, hits=int(hits or 0), refused=int(refused_hits or 0))
            for host, hits, refused_hits in host_rows
        ],
    )


async def status_payload() -> Status:
    from app.fork.traffic_log import collector

    payload = {
        **collector.status(),
        **collector.purge_stats,
        "retention_hours": await collector.effective_retention_hours(),
        "max_records": job_settings.traffic_log_max_records,
    }
    return Status.model_validate(payload)
