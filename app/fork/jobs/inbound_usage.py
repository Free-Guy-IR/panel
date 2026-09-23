import asyncio
from operator import attrgetter

from PasarGuardNodeBridge import NodeAPIError, PasarGuardNode
from PasarGuardNodeBridge.common.service_pb2 import StatType
from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import GetDB
from app.db.models import Node, NodeInboundUsage
from app.fork.jobs.stat_batches import (
    StaleStatBatches,
    StatBatch,
    as_ack_only,
    batch_from_response,
    held,
    load_acks,
    remember_epoch,
    request_keywords,
    settle_batches,
    supports_two_phase,
)
from app.utils.logger import get_logger

logger = get_logger("record-usages")


def _process_inbounds_stats_response(stats_response):
    totals: dict[str, dict[str, int]] = {}
    for stat in filter(attrgetter("value"), stats_response.stats):
        tag = getattr(stat, "name", None) or getattr(stat, "link", None)
        if not tag:
            continue
        entry = totals.setdefault(tag, {"up": 0, "down": 0})
        if stat.type == "uplink":
            entry["up"] += stat.value
        else:
            entry["down"] += stat.value
    return totals


async def get_inbounds_stats(node: PasarGuardNode):
    totals, _ = await _fetch_inbounds_stats(node)
    return totals


async def _fetch_inbounds_stats(
    node: PasarGuardNode, node_id: int | None = None, ack: StatBatch | None = None
) -> tuple[dict, StatBatch | None]:
    from app.jobs.record_usages import API_SEM, _get_thread_pool

    keywords = request_keywords(ack) if node_id is not None and supports_two_phase(node) else {}
    try:
        async with API_SEM:
            stats_response = await node.get_stats(stat_type=StatType.Inbounds, reset=True, timeout=10, **keywords)

        loop = asyncio.get_running_loop()
        thread_pool = await _get_thread_pool()
        totals = await loop.run_in_executor(thread_pool, _process_inbounds_stats_response, stats_response)
        batch = batch_from_response(stats_response) if node_id is not None else None
        if batch is not None:
            remember_epoch(node_id, StatType.Inbounds, batch)
        return totals, batch
    except NodeAPIError as e:
        if e.detail and "not applicable" in e.detail:
            logger.debug("Inbound stats unavailable for this backend: %s", e.detail)
        else:
            logger.error("Failed to get inbounds stats, error: %s", e.detail)
        return {}, None
    except Exception as e:
        logger.error("Failed to get inbounds stats, unknown error: %s", e)
        return {}, None


def _inbound_upsert(dialect: str, values: dict):
    if dialect == "postgresql":
        stmt = pg_insert(NodeInboundUsage).values(**values)
        return stmt.on_conflict_do_update(
            index_elements=["created_at", "node_id", "inbound_tag"],
            set_={
                "uplink": NodeInboundUsage.uplink + stmt.excluded.uplink,
                "downlink": NodeInboundUsage.downlink + stmt.excluded.downlink,
            },
        )
    if dialect in ("mysql", "mariadb"):
        stmt = mysql_insert(NodeInboundUsage).values(**values)
        return stmt.on_duplicate_key_update(
            uplink=NodeInboundUsage.uplink + stmt.inserted.uplink,
            downlink=NodeInboundUsage.downlink + stmt.inserted.downlink,
        )
    stmt = sqlite_insert(NodeInboundUsage).values(**values)
    return stmt.on_conflict_do_update(
        index_elements=["created_at", "node_id", "inbound_tag"],
        set_={
            "uplink": NodeInboundUsage.uplink + stmt.excluded.uplink,
            "downlink": NodeInboundUsage.downlink + stmt.excluded.downlink,
        },
    )


async def _load_inbound_acks(nodes) -> dict[int, StatBatch]:
    capable = [node_id for node_id, node in nodes if supports_two_phase(node)]
    if not capable:
        return {}
    try:
        async with GetDB() as db:
            return await load_acks(db, StatType.Inbounds, capable)
    except Exception:
        logger.exception("Could not read the last recorded inbound stats batches; asking without acknowledgements")
        return {}


async def _write_inbound_rows(rows: list[dict], created_at, stat_batches: dict[int, StatBatch]) -> None:
    async with GetDB() as db:
        try:
            dialect = db.bind.dialect.name
            await settle_batches(db, StatType.Inbounds, stat_batches)
            node_ids = sorted({row["node_id"] for row in rows})
            existing = (
                set((await db.execute(select(Node.id).where(Node.id.in_(node_ids)))).scalars()) if rows else set()
            )
            for row in rows:
                values = {
                    "created_at": created_at,
                    "node_id": row["node_id"] if row["node_id"] in existing else None,
                    "inbound_tag": row["inbound_tag"],
                    "uplink": row["up"],
                    "downlink": row["down"],
                }
                await db.execute(_inbound_upsert(dialect, values))
            await db.commit()
        except BaseException:
            await db.rollback()
            raise


async def record_inbound_usages(nodes, created_at):
    if not nodes:
        return

    acks = await _load_inbound_acks(nodes)
    results = await asyncio.gather(
        *[_fetch_inbounds_stats(node, node_id, acks.get(node_id)) for node_id, node in nodes],
        return_exceptions=True,
    )

    rows = []
    stat_batches: dict[int, StatBatch] = {}
    for (node_id, _), result in zip(nodes, results, strict=False):
        if isinstance(result, Exception):
            logger.warning("Failed to get inbounds stats for node %s: %s", node_id, result)
            continue
        totals, batch = result
        if batch is not None:
            stat_batches[node_id] = batch
        for tag, entry in totals.items():
            if entry["up"] or entry["down"]:
                rows.append({"node_id": node_id, "inbound_tag": tag, "up": entry["up"], "down": entry["down"]})

    stat_batches = held(stat_batches)
    if not rows and not stat_batches:
        return

    while True:
        try:
            await _write_inbound_rows(rows, created_at, stat_batches)
            break
        except StaleStatBatches as stale:
            logger.warning(
                "Node(s) %s sent inbound stats batches that were already recorded; acknowledging them only",
                sorted(stale.node_ids),
            )
            rows = [row for row in rows if row["node_id"] not in stale.node_ids]
            stat_batches = as_ack_only(stat_batches, stale.node_ids)

    logger.debug("Recorded inbound usage for %d inbound(s)", len(rows))


async def after_record_node_usages(nodes, created_at):
    try:
        await record_inbound_usages(nodes, created_at)
    except Exception:
        logger.exception("Inbound usage recording failed; node usage was still recorded")
