import asyncio
import os
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from app import scheduler
from app.db import GetDB
from app.fork.models.traffic_log import TrafficLogRecord
from app.utils.logger import get_logger
from config import job_settings, runtime_settings

logger = get_logger("jobs")

DELETE_CHUNK = 5_000
MAX_PER_RUN = 200_000
CEILING_TIME_BUDGET = 60.0
RECLAIM_TIME_BUDGET = 300.0
SQLITE_RECLAIM_STATEMENTS = ("PRAGMA wal_checkpoint(TRUNCATE)", "VACUUM", "PRAGMA wal_checkpoint(TRUNCATE)")
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm")

_late_reclaims: set[asyncio.Task] = set()
_reclaim_task: asyncio.Task | None = None


async def _delete_matching(db, condition, *, limit: int | None = MAX_PER_RUN, deadline: float | None = None):
    deleted = 0
    while limit is None or deleted < limit:
        if deadline is not None and time.monotonic() >= deadline:
            return deleted, True

        ids = (await db.execute(select(TrafficLogRecord.id).where(condition).limit(DELETE_CHUNK))).scalars().all()
        if not ids:
            return deleted, False

        await db.execute(delete(TrafficLogRecord).where(TrafficLogRecord.id.in_(ids)))
        await db.commit()
        deleted += len(ids)

        if len(ids) < DELETE_CHUNK:
            return deleted, False
    return deleted, True


async def purge_before(db, cutoff: datetime) -> tuple[int, bool]:
    return await _delete_matching(db, TrafficLogRecord.bucket_start < cutoff)


async def enforce_ceiling(db, threshold: int):
    if threshold <= 0:
        return 0, False
    return await _delete_matching(
        db,
        TrafficLogRecord.id <= threshold,
        limit=None,
        deadline=time.monotonic() + CEILING_TIME_BUDGET,
    )


def session_engine(db):
    engine = getattr(db, "bind", None)
    if engine is not None:
        return engine
    try:
        return db.get_bind()
    except Exception:
        return None


def dialect_of(engine) -> str:
    dialect = getattr(engine, "dialect", None)
    if dialect is None:
        dialect = getattr(getattr(engine, "sync_engine", None), "dialect", None)
    return getattr(dialect, "name", "") or ""


def _sqlite_path(engine) -> str | None:
    url = getattr(engine, "url", None) or getattr(getattr(engine, "sync_engine", None), "url", None)
    database = getattr(url, "database", None)
    if not database or database == ":memory:":
        return None
    return database


def _database_bytes(path: str | None) -> int | None:
    if path is None:
        return None
    try:
        total = os.stat(path).st_size
    except OSError:
        return None
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        try:
            total += os.stat(path + suffix).st_size
        except OSError:
            continue
    return total


def _checkpoint_completed(result) -> bool:
    try:
        row = result.fetchone()
    except Exception:
        return True
    if row is None:
        return True
    return not row[0]


async def _reclaim_sqlite(engine) -> bool:
    compacted = False
    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as connection:
        for statement in SQLITE_RECLAIM_STATEMENTS:
            try:
                result = await connection.exec_driver_sql(statement)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning(f"Traffic log storage reclamation could not run {statement}: {error!r}")
                continue
            if statement.startswith("PRAGMA wal_checkpoint") and not _checkpoint_completed(result):
                logger.warning(f"Traffic log storage reclamation: {statement} was blocked by another connection")
                continue
            if statement == "VACUUM":
                compacted = True
    return compacted


def _log_late_reclaim(task: asyncio.Task) -> None:
    _late_reclaims.discard(task)
    if task.cancelled():
        logger.warning("Traffic log storage reclamation was cancelled")
        return
    error = task.exception()
    if error is not None:
        logger.error(f"Traffic log storage reclamation failed: {error!r}")
    else:
        logger.info("Traffic log storage reclamation finished after the purge had already answered")


async def reclaim_storage(engine) -> tuple[bool, int | None]:
    if _reclaim_task is not None and not _reclaim_task.done():
        logger.warning("Traffic log storage reclamation is already running; this purge answers without it")
        return False, None
    return await _reclaim_once(engine)


async def _reclaim_once(engine) -> tuple[bool, int | None]:
    try:
        dialect = dialect_of(engine)
        if not dialect:
            logger.warning("Traffic log storage reclamation skipped: the database dialect could not be identified")
            return False, None
        if dialect != "sqlite":
            logger.info(
                f"Traffic log purge reclaimed no storage: {dialect} reuses freed pages by itself, "
                "so the panel does not compact it"
            )
            return False, None
        if not hasattr(engine, "sync_engine"):
            logger.warning("Traffic log storage reclamation skipped: the session is not bound to an async engine")
            return False, None
        path = _sqlite_path(engine)
        before = _database_bytes(path)
        global _reclaim_task
        task = asyncio.create_task(_reclaim_sqlite(engine), name="traffic-log-reclaim")
        _reclaim_task = task
        try:
            done, _ = await asyncio.wait({task}, timeout=RECLAIM_TIME_BUDGET)
        except asyncio.CancelledError:
            _late_reclaims.add(task)
            task.add_done_callback(_log_late_reclaim)
            raise
        if not done:
            _late_reclaims.add(task)
            task.add_done_callback(_log_late_reclaim)
            logger.warning(
                f"Traffic log storage reclamation is still running after {RECLAIM_TIME_BUDGET} seconds; "
                "the purge answers without it"
            )
            return False, None
        if not task.result():
            logger.warning("Traffic log storage reclamation did not compact the database")
            return False, None
        after = _database_bytes(path)
        if before is None or after is None:
            return True, None
        freed = max(before - after, 0)
        logger.info(f"Traffic log storage reclamation returned {freed} bytes to the filesystem")
        return True, freed
    except Exception:
        logger.exception("Traffic log storage reclamation failed")
        return False, None


async def purge_traffic_log():
    from app.fork.traffic_log import collector
    from app.fork.traffic_log.identity import forget_unreferenced_identities, reconcile_identities

    retention_hours = await collector.effective_retention_hours()
    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    ceiling = job_settings.traffic_log_max_records

    async with GetDB() as db:
        expired, expired_incomplete = await purge_before(db, cutoff)

        if ceiling > 0:
            max_id = (await db.execute(select(func.max(TrafficLogRecord.id)))).scalar()
            threshold = (max_id or 0) - ceiling
        else:
            threshold = 0
        over_ceiling, ceiling_incomplete = await enforce_ceiling(db, threshold)

        try:
            reconciled = await reconcile_identities(db)
        except Exception:
            logger.exception("Traffic log identity reconciliation failed")
            reconciled = 0

        try:
            forgotten = await forget_unreferenced_identities(db)
        except Exception:
            logger.exception("Traffic log identity cleanup failed")
            forgotten = 0

    incomplete = expired_incomplete or ceiling_incomplete
    collector.identity.prune()
    if expired or over_ceiling:
        collector.forget_buckets(cutoff)
    collector.note_ceiling_floor(threshold, ceiling_incomplete)
    collector.record_purge(expired, over_ceiling, over_ceiling > 0, incomplete)

    if expired or over_ceiling or reconciled or forgotten:
        logger.info(
            f"Traffic log purge removed {expired} records older than {retention_hours} hours, "
            f"{over_ceiling} records over the {ceiling} record ceiling, "
            f"reconciled {reconciled} identities and forgot {forgotten} unreferenced ones"
            + (" (more remain, continuing next run)" if incomplete else "")
        )


if runtime_settings.role.runs_scheduler:
    scheduler.add_job(
        purge_traffic_log,
        "interval",
        seconds=job_settings.traffic_log_purge_interval,
        max_instances=1,
        id="traffic_log_purge",
        replace_existing=True,
    )
