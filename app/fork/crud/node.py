from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.general import (
    _build_trunc_expression,
    attach_timezone_to_period_start,
    to_utc_for_filter,
)
from app.db.models import CoreConfig, NodeInboundUsage
from app.models.stats import InboundUsageStat, InboundUsageStatsList, Period


async def resolve_additional_cores(db: AsyncSession, core_ids: list[int] | None) -> list[CoreConfig]:
    if not core_ids:
        return []
    ordered = list(dict.fromkeys(core_ids))
    result = await db.execute(select(CoreConfig).where(CoreConfig.id.in_(ordered)))
    by_id = {core.id: core for core in result.scalars().all()}
    return [by_id[core_id] for core_id in ordered if core_id in by_id]


async def get_inbounds_usage(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    period: Period,
    inbound_tag: str | None = None,
    node_id: int | None = None,
) -> InboundUsageStatsList:
    """Per-inbound traffic grouped into period buckets.

    Deliberately the same shape and bucketing rules as get_nodes_usage - every row
    in range is returned exactly once and edge buckets are kept - so both series can
    be shown on the same axes without one being offset against the other.
    """
    dialect = db.bind.dialect.name

    trunc_expr = _build_trunc_expression(db, period, NodeInboundUsage.created_at, start)

    start_utc = to_utc_for_filter(start)
    end_utc = to_utc_for_filter(end)
    conditions = [NodeInboundUsage.created_at >= start_utc, NodeInboundUsage.created_at < end_utc]

    if inbound_tag:
        conditions.append(NodeInboundUsage.inbound_tag == inbound_tag)
    if node_id is not None:
        conditions.append(NodeInboundUsage.node_id == node_id)

    stmt = (
        select(
            trunc_expr.label("period_start"),
            NodeInboundUsage.inbound_tag.label("inbound_tag"),
            func.sum(NodeInboundUsage.downlink).label("downlink"),
            func.sum(NodeInboundUsage.uplink).label("uplink"),
        )
        .where(and_(*conditions))
        .group_by(trunc_expr, NodeInboundUsage.inbound_tag)
        .order_by(trunc_expr, NodeInboundUsage.inbound_tag)
    )

    result = await db.execute(stmt)

    stats: dict[str, list[InboundUsageStat]] = {}
    for row in result.mappings():
        row_dict = dict(row)
        tag = row_dict.pop("inbound_tag")
        attach_timezone_to_period_start(row_dict, start.tzinfo, dialect)
        stats.setdefault(tag, []).append(InboundUsageStat(**row_dict))

    return InboundUsageStatsList(period=period, start=start, end=end, stats=stats)
