from app.db import GetDB
from app.operation.node import NodeOperation
from app.utils.logger import get_logger

logger = get_logger("jobs")


async def after_healthy_node_check(node, db_node):
    async with GetDB() as extras_db:
        failed = await NodeOperation._reconcile_extra_cores(extras_db, node, db_node)
    if failed:
        logger.warning(f"[{db_node.name}] additional cores not fully reconciled: {failed}")
