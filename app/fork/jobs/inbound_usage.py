import asyncio
from operator import attrgetter

from PasarGuardNodeBridge import NodeAPIError, PasarGuardNode
from PasarGuardNodeBridge.common.service_pb2 import StatType
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import GetDB
from app.db.models import NodeInboundUsage
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
    from app.jobs.record_usages import API_SEM, _get_thread_pool

    try:
        async with API_SEM:
            stats_response = await node.get_stats(stat_type=StatType.Inbounds, reset=True, timeout=10)

        loop = asyncio.get_running_loop()
        thread_pool = await _get_thread_pool()
        return await loop.run_in_executor(thread_pool, _process_inbounds_stats_response, stats_response)
    except NodeAPIError as e:
        if e.detail and "not applicable" in e.detail:
            logger.debug("Inbound stats unavailable for this backend: %s", e.detail)
        else:
            logger.error("Failed to get inbounds stats, error: %s", e.detail)
        return {}
    except Exception as e:
        logger.error("Failed to get inbounds stats, unknown error: %s", e)
        return {}


async def record_inbound_usages(nodes, created_at):
    if not nodes:
        return

    results = await asyncio.gather(*[get_inbounds_stats(node) for _, node in nodes], return_exceptions=True)

    rows = []
    for (node_id, _), result in zip(nodes, results, strict=False):
        if isinstance(result, Exception):
            logger.warning("Failed to get inbounds stats for node %s: %s", node_id, result)
            continue
        for tag, totals in result.items():
            if totals["up"] or totals["down"]:
                rows.append({"node_id": node_id, "inbound_tag": tag, "up": totals["up"], "down": totals["down"]})

    if not rows:
        return

    async with GetDB() as db:
        dialect = db.bind.dialect.name
        for row in rows:
            values = {
                "created_at": created_at,
                "node_id": row["node_id"],
                "inbound_tag": row["inbound_tag"],
                "uplink": row["up"],
                "downlink": row["down"],
            }
            if dialect == "postgresql":
                stmt = pg_insert(NodeInboundUsage).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=["created_at", "node_id", "inbound_tag"],
                    set_={
                        "uplink": NodeInboundUsage.uplink + stmt.excluded.uplink,
                        "downlink": NodeInboundUsage.downlink + stmt.excluded.downlink,
                    },
                )
            elif dialect in ("mysql", "mariadb"):
                stmt = mysql_insert(NodeInboundUsage).values(**values)
                stmt = stmt.on_duplicate_key_update(
                    uplink=NodeInboundUsage.uplink + stmt.inserted.uplink,
                    downlink=NodeInboundUsage.downlink + stmt.inserted.downlink,
                )
            else:
                stmt = (
                    sqlite_insert(NodeInboundUsage)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=["created_at", "node_id", "inbound_tag"],
                        set_={
                            "uplink": NodeInboundUsage.uplink + sqlite_insert(NodeInboundUsage).excluded.uplink,
                            "downlink": NodeInboundUsage.downlink + sqlite_insert(NodeInboundUsage).excluded.downlink,
                        },
                    )
                )
            await db.execute(stmt)
        await db.commit()

    logger.debug("Recorded inbound usage for %d inbound(s)", len(rows))


async def after_record_node_usages(nodes, created_at):
    try:
        await record_inbound_usages(nodes, created_at)
    except Exception:
        logger.exception("Inbound usage recording failed; node usage was still recorded")
