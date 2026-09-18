import asyncio

from sqlalchemy import select

from app import scheduler
from app.db import GetDB
from app.db.models import Node, NodeStatus
from app.fork.content_filter import service
from app.fork.models.content_filter import ContentFilterAssignment
from app.node import node_manager
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("jobs")

RECONCILE_LIMIT = 5
RECONCILE_TIMEOUT = 30

_in_flight: set[int] = set()
_pending: set[asyncio.Task] = set()
_last_status_change: dict[int, object] = {}
_UNSEEN = object()
_sem: asyncio.Semaphore | None = None
_sem_loop: asyncio.AbstractEventLoop | None = None


def reconcile_budget() -> asyncio.Semaphore:
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(RECONCILE_LIMIT)
        _sem_loop = loop
        _in_flight.clear()
    return _sem


async def nodes_to_reconcile(db) -> list[int]:
    assignments = (
        (await db.execute(select(ContentFilterAssignment).where(ContentFilterAssignment.is_enabled.is_(True))))
        .scalars()
        .all()
    )
    if not assignments:
        return []

    pinned = {assignment.node_id for assignment in assignments if assignment.node_id is not None}
    floating = {assignment.inbound_tag for assignment in assignments if assignment.node_id is None}

    nodes = (await db.execute(select(Node).where(Node.status != NodeStatus.disabled))).scalars().all()

    targets: list[int] = []
    for node in nodes:
        if node.id in pinned:
            targets.append(node.id)
            continue
        if floating and (floating & await service.node_inbound_tags(db, node.id)):
            targets.append(node.id)
    return targets


async def _reconcile_one(node_id: int) -> None:
    budget = reconcile_budget()

    if node_id in _in_flight:
        logger.debug(f"Content filter reconcile skipped node {node_id}: one is already running for it")
        return

    if await node_manager.get_node(node_id) is None:
        logger.debug(f"Content filter reconcile skipped node {node_id}: it is not attached to this worker")
        return

    _in_flight.add(node_id)
    try:
        async with budget:
            try:
                async with GetDB() as db:
                    error = await asyncio.wait_for(service.reconcile_node(db, node_id), timeout=RECONCILE_TIMEOUT)
            except TimeoutError:
                logger.warning(
                    f"Content filter reconcile on node {node_id} did not finish within {RECONCILE_TIMEOUT}s "
                    "and was abandoned; the next run will try again"
                )
                return
            except Exception as exc:
                logger.warning(f"Content filter reconcile on node {node_id} failed: {exc!r}")
                return
    except Exception as exc:
        logger.warning(f"Content filter reconcile on node {node_id} could not be started: {exc!r}")
        return
    finally:
        _in_flight.discard(node_id)

    if error:
        logger.warning(f"Content filter reconcile left node {node_id} unenforced: {error}")


def _spawn(node_id: int) -> None:
    task = asyncio.create_task(_reconcile_one(node_id), name=f"content-filter-reconcile-{node_id}")
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def reconcile_after_reconnect(db_node) -> None:
    if not job_settings.content_filter_reconcile_enabled:
        return

    marker = db_node.last_status_change
    if _last_status_change.get(db_node.id, _UNSEEN) == marker:
        return

    _last_status_change[db_node.id] = marker
    _spawn(db_node.id)


async def reconcile_content_filter():
    if not job_settings.content_filter_reconcile_enabled:
        return

    try:
        async with GetDB() as db:
            node_ids = await nodes_to_reconcile(db)

        if not node_ids:
            return

        await asyncio.gather(*[_reconcile_one(node_id) for node_id in node_ids], return_exceptions=True)
    except Exception:
        logger.exception("Content filter reconciliation failed")


if job_settings.content_filter_reconcile_enabled and runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        reconcile_content_filter,
        "interval",
        seconds=job_settings.content_filter_reconcile_interval,
        max_instances=1,
        id="content_filter_reconcile",
        replace_existing=True,
    )
