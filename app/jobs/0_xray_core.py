import time
import traceback

from app import on_shutdown, on_startup, scheduler, backend
from app.db import GetDB, crud
from app.models.node import NodeStatus
from app.utils.logger import get_logger
from config import JOB_CORE_HEALTH_CHECK_INTERVAL
from xray_api import exc as xray_exc

logger = get_logger("xray-core")


def core_health_check():
    config = None

    # main core
    if not backend.core.started:
        if not config:
            config = backend.config.include_db_users()
        backend.core.restart(config)

    # nodes' core
    for node_id, node in list(backend.nodes.items()):
        if node.connected:
            try:
                assert node.started
                node.api.get_sys_stats(timeout=2)
            except (ConnectionError, xray_exc.XrayError, AssertionError):
                if not config:
                    config = backend.config.include_db_users()
                backend.operations.restart_node(node_id, config)

        if not node.connected:
            if not config:
                config = backend.config.include_db_users()
            backend.operations.connect_node(node_id, config)


@on_startup
def initialize_cores():
    logger.info("Generating config...")

    start_time = time.time()
    config = backend.config.include_db_users()
    logger.info(f"Config generated in {(time.time() - start_time):.2f} seconds")

    logger.info("Starting main and nodes' cores...")

    # main core
    try:
        backend.core.start(config)
    except Exception:
        traceback.print_exc()

    # nodes' core
    with GetDB() as db:
        dbnodes = crud.get_nodes(db=db, enabled=True)
        node_ids = [dbnode.id for dbnode in dbnodes]
        for dbnode in dbnodes:
            crud.update_node_status(db, dbnode, NodeStatus.connecting)

    for node_id in node_ids:
        backend.operations.connect_node(node_id, config)

    scheduler.add_job(
        core_health_check, "interval", seconds=JOB_CORE_HEALTH_CHECK_INTERVAL, coalesce=True, max_instances=1
    )


@on_shutdown
def shutdown_cores():
    logger.info("Stopping main and nodes' cores...")
    backend.core.stop()

    for node in list(backend.nodes.values()):
        try:
            node.disconnect()
        except Exception:
            pass
