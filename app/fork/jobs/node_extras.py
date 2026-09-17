from app.db import GetDB
from app.operation.node import NodeOperation
from app.utils.logger import get_logger

logger = get_logger("jobs")


async def _attach_traffic_log(node, db_node) -> None:
    from app.fork.traffic_log import collector

    try:
        await collector.ensure_attached(db_node.id, node, db_node.name)
    except Exception:
        logger.exception(f"[{db_node.name}] traffic log collector could not attach")


async def after_healthy_node_check(node, db_node):
    await _attach_traffic_log(node, db_node)
    async with GetDB() as extras_db:
        failed = await NodeOperation._reconcile_extra_cores(extras_db, node, db_node)
    if failed:
        logger.warning(f"[{db_node.name}] additional cores not fully reconciled: {failed}")
