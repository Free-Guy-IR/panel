import asyncio
import multiprocessing
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime as dt, timedelta as td
from operator import attrgetter
from typing import NamedTuple

from PasarGuardNodeBridge import NodeAPIError, PasarGuardNode
from PasarGuardNodeBridge.common.service_pb2 import StatType
from sqlalchemy import BigInteger, DateTime, bindparam, func, select, union_all, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import ARRAY, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.sql.expression import Insert

from app import on_shutdown, scheduler
from app.db import GetDB
from app.db.base import engine
from app.db.models import Admin, Node, NodeUsage, NodeUserUsage, System, User
from app.fork.jobs import after_record_node_usages, apply_usage_value
from app.node import node_manager
from app.operation.admin_sync import enforce_admin_limits_now
from app.utils.logger import get_logger
from config import job_settings, runtime_settings, usage_settings

logger = get_logger("record-usages")

# Hard-limit concurrency: Prevent DB lock storms
# Start with 2-4, adjust based on DB performance
JOB_SEM = asyncio.Semaphore(3)  # Max 3 concurrent DB write operations
API_SEM = asyncio.Semaphore(10)  # Max 10 concurrent node stats RPCs
USAGE_PERSIST_COHORT_SIZE = 8
USAGE_PERSIST_MAX_VOLATILE_S = 5.0
USAGE_PERSIST_MAX_CONSECUTIVE_FAILURES = 2
USAGE_JOB_LATENESS_TOLERANCE_S = 5.0
USAGE_RETAINED_MAX_ATTEMPTS = 10
NODE_USER_USAGE_BATCH_SIZE_BY_DIALECT = {
    "mysql": 1_000,
    "sqlite": 400,
}
USER_TRAFFIC_UPDATE_BATCH_SIZE_BY_DIALECT = {
    "mysql": 500,
    "sqlite": 400,
}
USER_ADMIN_LOOKUP_BATCH_SIZE = 1_000
DEADLOCK_MAX_RETRIES = 5
FENCED_USER_LOG_SAMPLE = 20
UNKNOWN_UID_LOG_SAMPLE = 20
UNKNOWN_UID_LOG_INTERVAL_S = 300.0


class UserUsageContext(NamedTuple):
    admin_id: int | None
    usage_epoch: int


class FencedUsageOutcome(NamedTuple):
    applied_users: int
    applied_admins: int
    fenced_user_ids: list[int]
    fenced_bytes: int


class UsageCohort(NamedTuple):
    api_params: dict
    usage_coefficient: dict
    epoch_at_poll: dict[int, int]
    attempts: int = 0


class PersistProgress:
    def __init__(self) -> None:
        self.writes_started = False


# Thread pool executor for I/O-bound node API calls
# Distributes workload across threads/cores for data collection
_thread_pool = None
_thread_pool_lock = asyncio.Lock()


async def _get_thread_pool():
    """Get or create the thread pool executor (thread-safe)."""
    global _thread_pool
    async with _thread_pool_lock:
        if _thread_pool is None:
            # Use more threads for I/O-bound operations (2x CPU cores, cap at 16)
            num_workers = min(multiprocessing.cpu_count() * 2, 16)
            _thread_pool = ThreadPoolExecutor(max_workers=num_workers)
            logger.debug(f"Initialized ThreadPoolExecutor with {num_workers} workers")
        return _thread_pool


@on_shutdown
async def _cleanup_thread_pool():
    """Cleanup thread pool on shutdown (thread-safe)."""
    global _thread_pool
    async with _thread_pool_lock:
        if _thread_pool is not None:
            logger.debug("Shutting down ThreadPoolExecutor...")
            _thread_pool.shutdown(wait=True)
            _thread_pool = None
            logger.debug("ThreadPoolExecutor shut down successfully")


# Helper functions for threading (lightweight operations that release GIL)
def _process_node_chunk(chunk_data: tuple) -> dict:
    """
    Process a chunk of node data - lightweight CPU operation.
    Uses simple arithmetic and dict operations that release GIL, perfect for threads.
    """
    _node_id, params, coeff = chunk_data
    users_usage = defaultdict(int)
    for param in params:
        uid = int(param["uid"])
        value = apply_usage_value(param["value"], coeff)
        users_usage[uid] += value
    return dict(users_usage)


def _merge_usage_dicts(dicts: list[dict]) -> dict:
    """
    Merge multiple usage dictionaries.
    Dict operations release GIL, perfect for ThreadPoolExecutor.
    """
    merged = defaultdict(int)
    for d in dicts:
        for uid, value in d.items():
            merged[uid] += value
    return dict(merged)

# Prevent overlapping usage jobs from stacking writes (and deadlocks) when
# node stats calls take longer than the scheduler interval.
_user_usage_running = False
_node_usage_running = False
_usage_coefficient_cache: dict[int, float] = {}
_usage_job_last_start: dict[str, float] = {}
_unknown_uid_total_count = 0
_unknown_uid_total_bytes = 0
_unknown_uid_last_log_at: float | None = None
_unknown_uid_suppressed_reports = 0
_retained_cohorts: list[UsageCohort] = []
_stats_reset_issued: ContextVar[set[int] | None] = ContextVar("usage_stats_reset_issued", default=None)


def _chunked(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


async def get_dialect() -> str:
    """Get the database dialect name. Cached after first call since the dialect never changes."""
    if _dialect_cache:
        return _dialect_cache[0]
    async with GetDB() as db:
        dialect = db.bind.dialect.name
    _dialect_cache.append(dialect)
    return dialect


# Simple one-element list used as a mutable cache container (set once, read many times)
_dialect_cache: list[str] = []


def build_node_user_usage_upsert(dialect: str, upsert_params: list[dict]):
    """
    Build UPSERT statement for NodeUserUsage based on database dialect.

    Args:
        dialect: Database dialect name ('postgresql', 'mysql', or 'sqlite')
        upsert_params: List of parameter dicts with keys: uid, node_id, created_at, value

    Returns:
        list: One SQL statement and its bound parameters.
    """
    if dialect == "postgresql":
        source = (
            func.unnest(
                bindparam("uids", type_=ARRAY(BigInteger())),
                bindparam("node_ids", type_=ARRAY(BigInteger())),
                bindparam("created_ats", type_=ARRAY(DateTime(timezone=True))),
                bindparam("traffic_values", type_=ARRAY(BigInteger())),
            )
            .table_valued("uid", "node_id", "created_at", "value")
            .render_derived(name="source")
        )

        select_stmt = (
            select(
                source.c.created_at,
                source.c.uid,
                source.c.node_id,
                func.sum(source.c.value).label("used_traffic"),
            )
            .select_from(source.join(User, User.id == source.c.uid))
            .group_by(source.c.created_at, source.c.uid, source.c.node_id)
        )

        stmt = pg_insert(NodeUserUsage).from_select(
            ["created_at", "user_id", "node_id", "used_traffic"],
            select_stmt,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["created_at", "user_id", "node_id"],
            set_={"used_traffic": NodeUserUsage.used_traffic + stmt.excluded.used_traffic},
        )
        return [
            (
                stmt,
                {
                    "uids": [param["uid"] for param in upsert_params],
                    "node_ids": [param["node_id"] for param in upsert_params],
                    "created_ats": [param["created_at"] for param in upsert_params],
                    "traffic_values": [param["value"] for param in upsert_params],
                },
            )
        ]

    select_parts = []
    stmt_params = {}
    for index, param in enumerate(upsert_params):
        uid_key = f"uid_{index}"
        node_id_key = f"node_id_{index}"
        created_at_key = f"created_at_{index}"
        value_key = f"value_{index}"
        select_parts.append(
            select(
                bindparam(uid_key).label("uid"),
                bindparam(node_id_key).label("node_id"),
                bindparam(created_at_key).label("created_at"),
                bindparam(value_key).label("value"),
            )
        )
        stmt_params[uid_key] = param["uid"]
        stmt_params[node_id_key] = param["node_id"]
        stmt_params[created_at_key] = param["created_at"]
        stmt_params[value_key] = param["value"]

    source = union_all(*select_parts).subquery("source")
    select_stmt = (
        select(
            source.c.created_at,
            source.c.uid,
            source.c.node_id,
            func.sum(source.c.value).label("used_traffic"),
        )
        .select_from(source.join(User, User.id == source.c.uid))
        .group_by(source.c.created_at, source.c.uid, source.c.node_id)
    )

    if dialect == "mysql":
        insert_source = select_stmt.subquery("insert_source")
        insert_select_stmt = select(
            insert_source.c.created_at,
            insert_source.c.uid,
            insert_source.c.node_id,
            insert_source.c.used_traffic,
        )
        stmt = mysql_insert(NodeUserUsage).from_select(
            ["created_at", "user_id", "node_id", "used_traffic"],
            insert_select_stmt,
        )
        stmt = stmt.on_duplicate_key_update(used_traffic=NodeUserUsage.used_traffic + stmt.inserted.used_traffic)
        return [(stmt, stmt_params)]

    stmt = sqlite_insert(NodeUserUsage).from_select(
        ["created_at", "user_id", "node_id", "used_traffic"],
        select_stmt,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["created_at", "user_id", "node_id"],
        set_={"used_traffic": NodeUserUsage.used_traffic + stmt.excluded.used_traffic},
    )
    return [(stmt, stmt_params)]


def build_node_usage_upsert(dialect: str, upsert_param: dict):
    """
    Build UPSERT statement for NodeUsage based on database dialect.

    Args:
        dialect: Database dialect name ('postgresql', 'mysql', or 'sqlite')
        upsert_param: Parameter dict with keys: node_id, created_at, up, down

    Returns:
        list: One (statement, params) pair for the dialect-specific upsert.
    """
    if dialect == "postgresql":
        stmt = pg_insert(NodeUsage).values(
            node_id=bindparam("node_id"),
            created_at=bindparam("created_at"),
            uplink=bindparam("up"),
            downlink=bindparam("down"),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["created_at", "node_id"],
            set_={
                "uplink": NodeUsage.uplink + bindparam("up"),
                "downlink": NodeUsage.downlink + bindparam("down"),
            },
        )
        return [(stmt, [upsert_param])]

    elif dialect == "mysql":
        stmt = mysql_insert(NodeUsage).values(
            node_id=bindparam("node_id"),
            created_at=bindparam("created_at"),
            uplink=bindparam("up"),
            downlink=bindparam("down"),
        )
        stmt = stmt.on_duplicate_key_update(
            uplink=NodeUsage.uplink + stmt.inserted.uplink,
            downlink=NodeUsage.downlink + stmt.inserted.downlink,
        )
        return [(stmt, [upsert_param])]

    else:  # SQLite
        stmt = sqlite_insert(NodeUsage).values(
            node_id=bindparam("node_id"),
            created_at=bindparam("created_at"),
            uplink=bindparam("up"),
            downlink=bindparam("down"),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["created_at", "node_id"],
            set_={
                "uplink": NodeUsage.uplink + stmt.excluded.uplink,
                "downlink": NodeUsage.downlink + stmt.excluded.downlink,
            },
        )
        return [(stmt, [upsert_param])]


def _mysql_errno(err) -> int | None:
    orig = getattr(err, "orig", err)
    args = getattr(orig, "args", None)
    if args and isinstance(args[0], int):
        return args[0]
    return None


def _is_retriable_db_error(err) -> bool:
    errno = _mysql_errno(err)
    if errno in (1213, 1205):
        return True
    orig = getattr(err, "orig", err)
    if getattr(orig, "code", None) == "40P01":
        return True
    message = str(err).lower()
    return "deadlock" in message or "lock wait timeout" in message or "database is locked" in message


async def run_in_retried_transaction(operation, max_retries: int = DEADLOCK_MAX_RETRIES):
    dialect = await get_dialect()
    connectable = engine
    if dialect == "mysql" and hasattr(engine, "execution_options"):
        # READ COMMITTED avoids gap/next-key locks that amplify MySQL deadlocks
        # during concurrent usage updates and upserts.
        connectable = engine.execution_options(isolation_level="READ COMMITTED")

    for attempt in range(max_retries):
        try:
            # engine.begin() ensures commit/rollback + connection return on exit
            async with connectable.begin() as conn:
                return await operation(conn)

        except (OperationalError, DatabaseError) as err:
            # Session auto-closed by context manager, locks released
            mysql_errno = _mysql_errno(err)
            is_sqlite_locked = "database is locked" in str(err).lower()

            if attempt < max_retries - 1 and _is_retriable_db_error(err):
                if is_sqlite_locked and attempt > 0:
                    # When SQLite is overloaded, extra retries become a self-DDOS
                    logger.warning("SQLite lock persisted after retry; dropping operation to prevent retry storm")
                    raise

                # Exponential backoff with jitter. Lock-wait timeouts get a longer base delay.
                base_delay = 0.2 * (2**attempt) if mysql_errno == 1205 else 0.1 * (2**attempt)
                jitter = random.uniform(0, base_delay * 0.5)
                logger.warning(
                    "Retrying usage write after %s (attempt %s/%s)",
                    f"MySQL {mysql_errno}" if mysql_errno else err.__class__.__name__,
                    attempt + 1,
                    max_retries,
                )
                await asyncio.sleep(base_delay + jitter)
                continue

            if attempt >= max_retries - 1 and _is_retriable_db_error(err):
                logger.error("Usage write failed after %s attempts: %s", max_retries, err)
            raise


async def safe_execute_many(statements: list[tuple], max_retries: int = DEADLOCK_MAX_RETRIES):
    """
    Execute multiple statements atomically in a single transaction.

    Same deadlock/lock retry handling as safe_execute; on any failure the whole
    batch rolls back so multi-table updates cannot be applied partially.

    Args:
        statements: List of (stmt, params) tuples; params may be None
        max_retries (int, optional): Maximum number of retry attempts
    """
    dialect = await get_dialect()
    prepared = []
    for stmt, params in statements:
        if (
            dialect == "mysql"
            and isinstance(stmt, Insert)
            and (not hasattr(stmt, "_post_values_clause") or stmt._post_values_clause is None)
        ):
            stmt = stmt.prefix_with("IGNORE")
        prepared.append((stmt, params))

    async def _execute_all(conn):
        for statement, statement_params in prepared:
            if statement_params is None:
                await conn.execute(statement)
            else:
                await conn.execute(statement, statement_params)

    await run_in_retried_transaction(_execute_all, max_retries=max_retries)


async def safe_execute(stmt, params=None, max_retries: int = DEADLOCK_MAX_RETRIES):
    """
    Safely execute database operations with deadlock and connection handling.
    Creates a fresh DB session for each retry attempt to release locks.

    Reduced retries to prevent retry amplification under load.
    Dropping some stats is better than crashing the system.

    Args:
        stmt: SQLAlchemy statement to execute
        params (list[dict], optional): Parameters for the statement
        max_retries (int, optional): Maximum number of retry attempts
    """
    await safe_execute_many([(stmt, params)], max_retries=max_retries)


def _get_time_bucket(now: dt | None = None) -> dt:
    """
    Get 10-minute time bucket instead of hourly to reduce hot row contention.
    This reduces lock contention by 6x (60 minutes / 10 minutes = 6).

    Args:
        now: Optional datetime to use (defaults to current time)

    Returns:
        datetime rounded down to 10-minute bucket
    """
    if now is None:
        now = dt.now(UTC)
    # Round down to 10-minute bucket: minute // 10 * 10
    return now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0)


async def record_user_stats_batched(all_node_params: dict, usage_coefficients: dict):
    """
    Record user statistics for ALL nodes in a single batched UPSERT operation.
    This eliminates per-node write amplification and reduces lock contention.

    Args:
        all_node_params: Dict mapping node_id -> list of user stat params
        usage_coefficients: Dict mapping node_id -> usage coefficient
    """
    if not all_node_params:
        return

    # Aggregate all params across all nodes into single list
    created_at = _get_time_bucket()
    dialect = await get_dialect()

    # Prepare parameters for all nodes in one batch
    upsert_params = []
    for node_id, params in all_node_params.items():
        if not params:
            continue
        coeff = usage_coefficients.get(node_id, 1.0)
        for p in params:
            upsert_params.append(
                {
                    "uid": int(p["uid"]),
                    "value": apply_usage_value(p["value"], coeff),
                    "node_id": node_id,
                    "created_at": created_at,
                }
            )

    if not upsert_params:
        return

    # Consistent lock order reduces InnoDB deadlocks across overlapping writers
    upsert_params.sort(key=lambda item: (item["uid"], item["node_id"]))

    batch_size = NODE_USER_USAGE_BATCH_SIZE_BY_DIALECT.get(dialect, len(upsert_params))
    batches = list(_chunked(upsert_params, batch_size))
    if len(batches) > 1:
        logger.debug(
            "Splitting %s node user usage rows into %s %s batches",
            len(upsert_params),
            len(batches),
            dialect,
        )

    # Execute batched UPSERTs with concurrency control
    async with JOB_SEM:
        for batch in batches:
            queries = build_node_user_usage_upsert(dialect, batch)
            for stmt, stmt_params in queries:
                await safe_execute(stmt, stmt_params)


async def record_node_stats_batched(all_node_params: dict):
    """
    Record node-level statistics for ALL nodes in batched operations.
    This reduces write amplification and lock contention.

    Args:
        all_node_params: Dict mapping node_id -> list of node stat params
    """
    if not all_node_params:
        return

    created_at = _get_time_bucket()
    dialect = await get_dialect()

    # Process each node's stats with concurrency control
    async def _record_single_node(node_id: int, params: list[dict]):
        if not params:
            return

        # Aggregate uplink and downlink from params
        total_up = sum(p.get("up", 0) for p in params)
        total_down = sum(p.get("down", 0) for p in params)

        if not (total_up or total_down):
            return

        upsert_param = {
            "node_id": node_id,
            "created_at": created_at,
            "up": total_up,
            "down": total_down,
        }

        # Execute with concurrency control
        async with JOB_SEM:
            queries = build_node_usage_upsert(dialect, upsert_param)
            for stmt, stmt_params in queries:
                await safe_execute(stmt, stmt_params)

    # Execute all node stats with limited concurrency
    tasks = [_record_single_node(node_id, params) for node_id, params in all_node_params.items()]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _process_users_stats_response(stats_response):
    """
    Process stats response (CPU-bound operation) - runs in thread pool.
    Pure function designed for thread-safe execution.
    Returns tuple: (validated_params, invalid_uids) for logging outside thread.
    """
    params = defaultdict(int)
    for stat in filter(attrgetter("value"), stats_response.stats):
        params[stat.name] += stat.value

    validated_params = []
    invalid_uids = []
    for uid, value in params.items():
        try:
            validated_params.append({"uid": int(uid), "value": value})
        except (ValueError, TypeError):
            invalid_uids.append(uid)

    return validated_params, invalid_uids


def _usage_job_hint(interval_env: str, interval: int) -> str:
    return (
        f"Lengthen {interval_env} (currently {interval}s) or cut node stats RPC latency. "
        "Raising UVICORN_WORKERS will not help — only one worker records usage."
    )


async def _await_usage_job(job_name: str, impl, interval: int, interval_env: str) -> None:
    # No global wait_for kill: get_stats uses reset=True, so cancelling mid-run drops traffic.
    start = time.monotonic()
    previous_start = _usage_job_last_start.get(job_name)
    _usage_job_last_start[job_name] = start
    if previous_start is not None and interval > 0:
        gap = start - previous_start
        lateness = gap - interval
        if lateness > USAGE_JOB_LATENESS_TOLERANCE_S:
            logger.warning(
                "%s started %.1fs late: %.1fs since the previous run against a %ss interval",
                job_name,
                lateness,
                gap,
                interval,
            )
    try:
        await impl()
    except asyncio.CancelledError:
        logger.warning("%s was cancelled", job_name)
    elapsed = time.monotonic() - start
    if interval > 0 and elapsed > interval:
        logger.warning(
            "%s took %.1fs which exceeds the %ss interval; later ticks will be skipped until this run finishes. %s",
            job_name,
            elapsed,
            interval,
            _usage_job_hint(interval_env, interval),
        )


async def _node_usage_coefficient(node: PasarGuardNode, node_id: int) -> float:
    try:
        extra = await node.get_extra()
    except Exception as exc:
        last_known = _usage_coefficient_cache.get(node_id)
        if last_known is None:
            logger.error(
                "No usage coefficient available for node %s (%s); billing this cycle at 1.0",
                node_id,
                exc,
            )
            return 1.0
        logger.warning(
            "Failed to read usage coefficient for node %s (%s); reusing last known value %s",
            node_id,
            exc,
            last_known,
        )
        return last_known

    raw_coeff = extra.get("usage_coefficient") if extra else None
    if raw_coeff is None:
        last_known = _usage_coefficient_cache.get(node_id)
        if last_known is not None:
            logger.error("Node %s reported no usage coefficient; reusing last known value %s", node_id, last_known)
            return last_known
        logger.error("Node %s reported no usage coefficient and none was ever seen; billing at 1.0", node_id)
        return 1.0

    try:
        coeff = float(raw_coeff)
    except (TypeError, ValueError):
        last_known = _usage_coefficient_cache.get(node_id)
        if last_known is not None:
            logger.error(
                "Node %s reported an unusable usage coefficient %r; reusing last known value %s",
                node_id,
                raw_coeff,
                last_known,
            )
            return last_known
        logger.error(
            "Node %s reported an unusable usage coefficient %r and none was ever seen; billing at 1.0",
            node_id,
            raw_coeff,
        )
        return 1.0

    _usage_coefficient_cache[node_id] = coeff
    return coeff


async def _collect_node_user_usage(node: PasarGuardNode, node_id: int) -> tuple[int, float, list]:
    """Fetch coefficient and user stats under one RPC slot so extra+stats overlap."""
    async with API_SEM:
        coeff_result, stats_result = await asyncio.gather(
            _node_usage_coefficient(node, node_id),
            get_users_stats(node, node_id),
            return_exceptions=True,
        )
    if isinstance(coeff_result, Exception):
        logger.warning("Failed to get extra data for node %s: %s", node_id, coeff_result)
        coeff = 1.0
    else:
        coeff = coeff_result
    if isinstance(stats_result, Exception):
        logger.warning("Failed to get stats for node %s: %s", node_id, stats_result)
        stats: list = []
    else:
        stats = stats_result
    return node_id, coeff, stats


async def _bounded_node_rpc(coro):
    async with API_SEM:
        return await coro


async def get_users_stats(node: PasarGuardNode, node_id: int | None = None):
    """Fetch and fold user stats from one node. Dict folding stays on the event loop."""
    node_label = node_id if node_id is not None else getattr(node, "node_id", "unknown")
    try:
        reset_issued = _stats_reset_issued.get()
        if reset_issued is not None and node_id is not None:
            reset_issued.add(node_id)
        # Caller holds API_SEM so extra+stats can share one slot without deadlock.
        stats_response = await node.get_stats(stat_type=StatType.UsersStat, reset=True, timeout=30)
        validated_params, invalid_uids = _process_users_stats_response(stats_response)

        if invalid_uids:
            for uid in invalid_uids:
                logger.warning("Skipping invalid UID: %s", uid)

        return validated_params
    except NodeAPIError as e:
        logger.error("Failed to get users stats from node %s, error: %s", node_label, e.detail)
        return []
    except Exception as e:
        logger.error("Failed to get users stats from node %s, unknown error: %s", node_label, e)
        return []


def _process_outbounds_stats_response(stats_response):
    """Fold outbound uplink/downlink stats into per-row params."""
    params = [
        {"up": stat.value, "down": 0} if stat.type == "uplink" else {"up": 0, "down": stat.value}
        for stat in filter(attrgetter("value"), stats_response.stats)
    ]
    return params


async def get_outbounds_stats(node: PasarGuardNode, node_id: int | None = None):
    """Fetch and fold outbound stats from one node. Dict folding stays on the event loop."""
    node_label = node_id if node_id is not None else getattr(node, "node_id", "unknown")
    try:
        # Caller holds API_SEM so node RPCs stay bounded.
        stats_response = await node.get_stats(stat_type=StatType.Outbounds, reset=True, timeout=10)
        return _process_outbounds_stats_response(stats_response)
    except NodeAPIError as e:
        logger.error("Failed to get outbounds stats from node %s, error: %s", node_label, e.detail)
        return []
    except Exception as e:
        logger.error("Failed to get outbounds stats from node %s, unknown error: %s", node_label, e)
        return []


async def load_user_usage_context(uids: set[int]) -> dict[int, UserUsageContext]:
    context: dict[int, UserUsageContext] = {}
    if not uids:
        return context

    async with GetDB() as db:
        for uid_batch in _chunked(sorted(uids), USER_ADMIN_LOOKUP_BATCH_SIZE):
            stmt = select(User.id, User.admin_id, User.usage_epoch).where(User.id.in_(uid_batch))
            result = await db.execute(stmt)
            for user_id, admin_id, usage_epoch in result.fetchall():
                context[int(user_id)] = UserUsageContext(admin_id=admin_id, usage_epoch=int(usage_epoch or 0))

    return context


async def calculate_users_usage(api_params: dict, usage_coefficient: dict) -> list:
    """Aggregate user usage across nodes with coefficients applied."""
    if not api_params:
        return []

    def _process_usage_sync(chunks_data: list[tuple[int, list[dict], float]]):
        """Synchronous fallback used for small batches or on executor failures."""
        users_usage = defaultdict(int)
        for _, params, coeff in chunks_data:
            for param in params:
                uid = int(param["uid"])
                value = apply_usage_value(param["value"], coeff)
                users_usage[uid] += value
        return [{"uid": uid, "value": value} for uid, value in users_usage.items()]

    # Prepare chunks for parallel processing
    chunks = [
        (node_id, params, usage_coefficient.get(node_id, 1))
        for node_id, params in api_params.items()
        if params  # Skip empty params
    ]

    if not chunks:
        return []

    # For small datasets, process synchronously to avoid overhead
    total_params = sum(len(params) for _, params, _ in chunks)
    if total_params < 1000:
        return _process_usage_sync(chunks)

    # Large dataset - use ThreadPoolExecutor (faster for lightweight operations)
    loop = asyncio.get_running_loop()
    try:
        thread_pool = await _get_thread_pool()
    except Exception:
        logger.exception("Falling back to synchronous user usage calculation: failed to init thread pool")
        return _process_usage_sync(chunks)

    try:
        # Process chunks in parallel using threads (less overhead than processes)
        tasks = [loop.run_in_executor(thread_pool, _process_node_chunk, chunk) for chunk in chunks]
        chunk_results = await asyncio.gather(*tasks)

        # Merge results - also lightweight, use threads
        if len(chunk_results) > 4:
            # Split merge operation into smaller chunks
            chunk_size = max(1, len(chunk_results) // 4)
            merge_chunks = [chunk_results[i : i + chunk_size] for i in range(0, len(chunk_results), chunk_size)]
            merge_tasks = [
                loop.run_in_executor(thread_pool, _merge_usage_dicts, merge_chunk) for merge_chunk in merge_chunks
            ]
            partial_results = await asyncio.gather(*merge_tasks)
            final_result = _merge_usage_dicts(partial_results)
        else:
            final_result = _merge_usage_dicts(chunk_results)

        return [{"uid": uid, "value": value} for uid, value in final_result.items()]
    except Exception:
        logger.exception("Falling back to synchronous user usage calculation: executor merge failed")
        return _process_usage_sync(chunks)


async def current_usage_epochs() -> dict[int, int]:
    async with GetDB() as db:
        result = await db.execute(select(User.id, User.usage_epoch))
        return {int(user_id): int(usage_epoch or 0) for user_id, usage_epoch in result.fetchall()}


def stale_epoch_user_ids(user_context: dict[int, UserUsageContext], epoch_at_poll: dict[int, int]) -> set[int]:
    return {uid for uid, entry in user_context.items() if entry.usage_epoch != epoch_at_poll.get(uid, 0)}


def discard_reset_users(users_usage: list, api_params: dict, reset_user_ids) -> tuple[list, dict, int, int]:
    kept = []
    dropped_bytes = 0
    dropped_users = 0
    for usage in users_usage:
        if int(usage["uid"]) in reset_user_ids:
            dropped_bytes += usage["value"]
            dropped_users += 1
        else:
            kept.append(usage)

    if not dropped_users:
        return users_usage, api_params, 0, 0

    kept_params = {
        node_id: [param for param in params if int(param["uid"]) not in reset_user_ids]
        for node_id, params in api_params.items()
    }
    return kept, kept_params, dropped_bytes, dropped_users


async def apply_fenced_user_usage(
    users_usage: list,
    user_context: dict[int, UserUsageContext],
    epoch_at_poll: dict[int, int],
) -> FencedUsageOutcome:
    delta_by_user = {int(item["uid"]): int(item["value"]) for item in users_usage}
    user_ids = sorted(delta_by_user)
    dialect = await get_dialect()
    batch_size = USER_TRAFFIC_UPDATE_BATCH_SIZE_BY_DIALECT.get(dialect) or len(user_ids) or 1
    online_at = dt.now(UTC)

    user_stmt = (
        update(User)
        .where(User.id == bindparam("uid"), User.usage_epoch == bindparam("epoch"))
        .values(used_traffic=User.used_traffic + bindparam("value"), online_at=online_at)
        .execution_options(synchronize_session=False)
    )
    admin_stmt = (
        update(Admin)
        .where(Admin.id == bindparam("admin_id"))
        .values(used_traffic=Admin.used_traffic + bindparam("value"))
        .execution_options(synchronize_session=False)
    )

    async def _apply(conn) -> FencedUsageOutcome:
        committed_epochs: dict[int, int] = {}
        for id_batch in _chunked(user_ids, batch_size):
            locked = await conn.execute(
                select(User.id, User.usage_epoch).where(User.id.in_(id_batch)).order_by(User.id).with_for_update()
            )
            for user_id, usage_epoch in locked.fetchall():
                committed_epochs[int(user_id)] = int(usage_epoch or 0)

        billable = []
        fenced_user_ids = []
        fenced_bytes = 0
        for user_id in user_ids:
            usage_epoch = committed_epochs.get(user_id)
            if usage_epoch is None or usage_epoch != epoch_at_poll.get(user_id, 0):
                fenced_user_ids.append(user_id)
                fenced_bytes += delta_by_user[user_id]
                continue
            billable.append({"uid": user_id, "epoch": usage_epoch, "value": delta_by_user[user_id]})

        admin_totals = defaultdict(int)
        for item in billable:
            entry = user_context.get(item["uid"])
            if entry is not None and entry.admin_id:
                admin_totals[entry.admin_id] += item["value"]
        admin_data = [{"admin_id": admin_id, "value": value} for admin_id, value in sorted(admin_totals.items())]

        for update_batch in _chunked(billable, batch_size):
            await conn.execute(user_stmt, update_batch)
        if admin_data:
            await conn.execute(admin_stmt, admin_data)

        return FencedUsageOutcome(len(billable), len(admin_data), fenced_user_ids, fenced_bytes)

    return await run_in_retried_transaction(_apply)


def _report_unknown_uid_usage(
    users_usage: list, valid_user_ids: set[int], api_params: dict | None = None
) -> tuple[int, int]:
    global _unknown_uid_total_count, _unknown_uid_total_bytes
    global _unknown_uid_last_log_at, _unknown_uid_suppressed_reports

    unknown_set = set()
    unknown_bytes = 0
    for usage in users_usage:
        uid = int(usage["uid"])
        if uid in valid_user_ids:
            continue
        unknown_set.add(uid)
        unknown_bytes += int(usage["value"])

    if not unknown_set:
        return 0, 0

    _unknown_uid_total_count += len(unknown_set)
    _unknown_uid_total_bytes += unknown_bytes

    now = time.monotonic()
    if _unknown_uid_last_log_at is not None and now - _unknown_uid_last_log_at < UNKNOWN_UID_LOG_INTERVAL_S:
        _unknown_uid_suppressed_reports += 1
        return len(unknown_set), unknown_bytes

    offending_nodes = sorted(
        node_id
        for node_id, params in (api_params or {}).items()
        if any(int(param["uid"]) in unknown_set for param in params)
    )
    logger.warning(
        "Dropped %s bytes of usage for %s distinct uid(s) with no user row on node(s) %s; "
        "those nodes are still serving users the panel deleted. Sample: %s. "
        "Since start: %s distinct-uid hits, %s bytes, %s suppressed report(s)",
        unknown_bytes,
        len(unknown_set),
        offending_nodes[:UNKNOWN_UID_LOG_SAMPLE],
        sorted(unknown_set)[:UNKNOWN_UID_LOG_SAMPLE],
        _unknown_uid_total_count,
        _unknown_uid_total_bytes,
        _unknown_uid_suppressed_reports,
    )
    _unknown_uid_last_log_at = now
    _unknown_uid_suppressed_reports = 0
    return len(unknown_set), unknown_bytes


async def _drain_node_collection(tasks: list, cohort_size: int, max_volatile_s: float):
    completed_at: dict = {}
    pending = set(tasks)
    for task in pending:
        task.add_done_callback(lambda finished: completed_at.setdefault(finished, time.monotonic()))

    buffered: list = []

    def _volatile_age() -> float:
        now = time.monotonic()
        return now - min(completed_at.get(task, now) for task in buffered)

    while pending or buffered:
        if pending:
            timeout = max(0.0, max_volatile_s - _volatile_age()) if buffered else None
            done, pending = await asyncio.wait(pending, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            buffered.extend(done)

        if not buffered:
            continue

        if pending and _volatile_age() < max_volatile_s and (cohort_size <= 0 or len(buffered) < cohort_size):
            continue

        yield buffered
        buffered = []


async def _persist_collected_usage(
    api_params: dict,
    usage_coefficient: dict,
    epoch_at_poll: dict[int, int],
    progress: PersistProgress | None = None,
) -> tuple[FencedUsageOutcome, int]:
    users_usage = await calculate_users_usage(api_params, usage_coefficient)
    if not users_usage:
        logger.debug("No user usage to record")
        return FencedUsageOutcome(0, 0, [], 0), 0

    user_context = await load_user_usage_context({int(usage["uid"]) for usage in users_usage})
    _report_unknown_uid_usage(users_usage, set(user_context), api_params)
    if not user_context:
        logger.warning("Skipping user usage recording; no matching users found for received stats")
        return FencedUsageOutcome(0, 0, [], 0), 0

    reset_user_ids = stale_epoch_user_ids(user_context, epoch_at_poll)
    if reset_user_ids:
        users_usage, api_params, dropped_bytes, dropped_users = discard_reset_users(
            users_usage, api_params, reset_user_ids
        )
        if dropped_users:
            logger.warning(
                "Discarded %s bytes of in-flight usage for %s user(s) reset mid-collection",
                dropped_bytes,
                dropped_users,
            )
        if not users_usage:
            logger.debug("No user usage to record after discarding reset users")
            return FencedUsageOutcome(0, 0, [], 0), 0

    valid_user_ids = set(user_context)
    valid_users_usage = [usage for usage in users_usage if int(usage["uid"]) in valid_user_ids and usage["value"] > 0]

    filtered_node_params = {}
    if not usage_settings.disable_recording_node_usage:
        for node_id, params in api_params.items():
            filtered_params = [param for param in params if int(param["uid"]) in valid_user_ids]
            if filtered_params:
                filtered_node_params[node_id] = filtered_params

    if filtered_node_params:
        if progress is not None:
            progress.writes_started = True
        await record_user_stats_batched(filtered_node_params, usage_coefficient)
        total_records = sum(len(params) for params in filtered_node_params.values())
        logger.debug(f"Recorded {total_records} node user usage records across {len(filtered_node_params)} nodes")

    outcome = FencedUsageOutcome(0, 0, [], 0)
    if valid_users_usage:
        valid_users_usage.sort(key=lambda item: int(item["uid"]))
        if progress is not None:
            progress.writes_started = True
        async with JOB_SEM:
            outcome = await apply_fenced_user_usage(valid_users_usage, user_context, epoch_at_poll)
        if outcome.fenced_user_ids:
            logger.warning(
                "Usage epoch fence rejected %s bytes for %s user(s) reset mid-apply: %s",
                outcome.fenced_bytes,
                len(outcome.fenced_user_ids),
                outcome.fenced_user_ids[:FENCED_USER_LOG_SAMPLE],
            )
        logger.debug(f"Updated {outcome.applied_users} users and {outcome.applied_admins} admins")

    return outcome, len(filtered_node_params)


def _cohort_from_tasks(tasks, node_by_task: dict, epoch_at_poll: dict[int, int]) -> UsageCohort:
    usage_coefficient = {}
    api_params = {}
    for task in tasks:
        node_id = node_by_task[task]
        failure = asyncio.CancelledError() if task.cancelled() else task.exception()
        if failure is not None:
            logger.warning("Failed to collect usage for node %s: %s", node_id, failure)
            usage_coefficient[node_id] = 1.0
            api_params[node_id] = []
            continue
        _, coeff, stats = task.result()
        usage_coefficient[node_id] = coeff
        api_params[node_id] = stats
    return UsageCohort(api_params, usage_coefficient, epoch_at_poll)


def _raw_bytes(api_params: dict) -> int:
    return sum(int(param["value"]) for params in api_params.values() for param in params)


def _retain_finished_collections(tasks, node_by_task: dict, epoch_at_poll: dict[int, int]) -> None:
    finished = [task for task in tasks if task.done() and not task.cancelled() and task.exception() is None]
    if not finished:
        return
    cohort = _cohort_from_tasks(finished, node_by_task, epoch_at_poll)
    read_params = {node_id: params for node_id, params in cohort.api_params.items() if params}
    if not read_params:
        return
    _retained_cohorts.append(
        UsageCohort(
            read_params,
            {node_id: cohort.usage_coefficient[node_id] for node_id in read_params},
            epoch_at_poll,
        )
    )
    logger.warning(
        "Kept %s raw bytes already read and reset on node(s) %s for the next cycle",
        _raw_bytes(read_params),
        sorted(read_params),
    )


async def _settle_collection(
    node_by_task: dict, reset_issued: set[int], processed: set, epoch_at_poll: dict[int, int]
) -> None:
    for task, node_id in node_by_task.items():
        if not task.done() and node_id not in reset_issued:
            task.cancel()
    unfinished = [task for task in node_by_task if not task.done()]
    try:
        if unfinished:
            await asyncio.wait(unfinished)
    finally:
        _retain_finished_collections(
            [task for task in node_by_task if task not in processed], node_by_task, epoch_at_poll
        )


def _keep_or_abandon_retained(cohort: UsageCohort, progress: PersistProgress) -> None:
    attempts = cohort.attempts + 1
    if not progress.writes_started and attempts < USAGE_RETAINED_MAX_ATTEMPTS:
        _retained_cohorts.insert(0, cohort._replace(attempts=attempts))
        logger.warning(
            "Still holding %s raw bytes from node(s) %s after %s failed attempt(s); nothing was written",
            _raw_bytes(cohort.api_params),
            sorted(cohort.api_params),
            attempts,
        )
        return
    logger.error(
        "Gave up on %s raw bytes read from node(s) %s after %s attempt(s); %s",
        _raw_bytes(cohort.api_params),
        sorted(cohort.api_params),
        attempts,
        "a write had already started, so a retry could count them twice"
        if progress.writes_started
        else "the retry limit was reached",
    )


async def _persist_retained_cohorts(persist) -> bool:
    while _retained_cohorts:
        cohort = _retained_cohorts.pop(0)
        progress = PersistProgress()
        try:
            failure = await persist(cohort, progress)
        except BaseException:
            _keep_or_abandon_retained(cohort, progress)
            raise
        if failure is not None:
            _keep_or_abandon_retained(cohort, progress)
            return False
    return True


async def _record_user_usages_impl():
    """
    Internal implementation of record_user_usages.
    Separated to allow timeout wrapper.
    """
    job_start_time = time.time()
    nodes: tuple[int, PasarGuardNode] = await node_manager.get_healthy_nodes()

    if not nodes and not _retained_cohorts:
        logger.debug("No healthy nodes found, skipping user usage recording")
        return

    logger.debug(f"Starting user usage recording for {len(nodes)} nodes")

    try:
        epoch_at_poll = await current_usage_epochs()

        applied_users = 0
        applied_admins = 0
        recorded_nodes = 0
        persist_failure: Exception | None = None
        consecutive_persist_failures = 0

        async def persist(cohort: UsageCohort, progress: PersistProgress | None = None) -> Exception | None:
            nonlocal applied_users, applied_admins, recorded_nodes, persist_failure, consecutive_persist_failures
            try:
                outcome, persisted_nodes = await _persist_collected_usage(
                    cohort.api_params, cohort.usage_coefficient, cohort.epoch_at_poll, progress=progress
                )
            except Exception as exc:
                if persist_failure is None:
                    persist_failure = exc
                consecutive_persist_failures += 1
                logger.exception("Failed to persist usage for node(s) %s", sorted(cohort.api_params))
                return exc
            consecutive_persist_failures = 0
            applied_users += outcome.applied_users
            applied_admins += outcome.applied_admins
            recorded_nodes += persisted_nodes
            return None

        def must_stop(failure: Exception | None) -> bool:
            if failure is None or (
                _is_retriable_db_error(failure)
                and consecutive_persist_failures < USAGE_PERSIST_MAX_CONSECUTIVE_FAILURES
            ):
                return False
            logger.error(
                "Stopping collection after %s consecutive persistence failure(s); "
                "nodes not yet polled keep their counters for the next cycle",
                consecutive_persist_failures,
            )
            return True

        if await _persist_retained_cohorts(persist) and nodes:
            reset_issued: set[int] = set()
            token = _stats_reset_issued.set(reset_issued)
            try:
                node_by_task = {
                    asyncio.ensure_future(_collect_node_user_usage(node, node_id)): node_id for node_id, node in nodes
                }
            finally:
                _stats_reset_issued.reset(token)

            processed: set = set()
            try:
                async for batch in _drain_node_collection(
                    list(node_by_task), USAGE_PERSIST_COHORT_SIZE, USAGE_PERSIST_MAX_VOLATILE_S
                ):
                    processed.update(batch)
                    if must_stop(await persist(_cohort_from_tasks(batch, node_by_task, epoch_at_poll))):
                        break
            finally:
                await _settle_collection(node_by_task, reset_issued, processed, epoch_at_poll)

        if applied_admins:
            try:
                await enforce_admin_limits_now(logger=logger)
            except Exception:
                logger.exception("Failed to enforce admin limits after usage recording")

        if persist_failure is not None:
            raise persist_failure

        job_duration = time.time() - job_start_time
        logger.info(
            f"User usage recording completed in {job_duration:.2f}s: "
            f"{applied_users} users, {applied_admins} admins, "
            f"{recorded_nodes} nodes"
        )

    except Exception:
        job_duration = time.time() - job_start_time
        logger.exception(f"User usage recording failed after {job_duration:.2f}s")
        raise


async def record_user_usages():
    """Record user usages. Overlapping ticks are skipped; there is no global kill.

    ``get_stats(..., reset=True)`` zeros node counters, so a 120s cancel after
    that drop can lose traffic. If this job is skipped, lengthen
    JOB_RECORD_USER_USAGES_INTERVAL or cut node RPC latency — extra Uvicorn
    workers will not help.
    """
    global _user_usage_running
    if _user_usage_running:
        logger.warning(
            "record_user_usages skipped; previous run still in progress. %s",
            _usage_job_hint("JOB_RECORD_USER_USAGES_INTERVAL", job_settings.record_user_usages_interval),
        )
        return

    _user_usage_running = True
    try:
        await _await_usage_job(
            "record_user_usages",
            _record_user_usages_impl,
            job_settings.record_user_usages_interval,
            "JOB_RECORD_USER_USAGES_INTERVAL",
        )
    finally:
        _user_usage_running = False


async def _record_node_usages_impl():
    """
    Internal implementation of record_node_usages.
    Separated to allow timeout wrapper.
    """
    job_start_time = time.time()
    nodes = await node_manager.get_healthy_nodes()

    if not nodes:
        logger.debug("No healthy nodes found, skipping node usage recording")
        return

    logger.debug(f"Starting node usage recording for {len(nodes)} nodes")

    try:
        # Get healthy nodes and gather stats directly
        stats_results = await asyncio.gather(
            *[_bounded_node_rpc(get_outbounds_stats(node, node_id)) for node_id, node in nodes],
            return_exceptions=True,
        )
        api_params = {}
        for i, result in enumerate(stats_results):
            node_id = nodes[i][0]
            if isinstance(result, Exception):
                logger.warning(f"Failed to get outbounds stats for node {node_id}: {result}")
                api_params[node_id] = []
            else:
                api_params[node_id] = result

        # Calculate per-node totals
        node_totals = {
            node_id: {
                "up": sum(param["up"] for param in params),
                "down": sum(param["down"] for param in params),
            }
            for node_id, params in api_params.items()
        }

        # Calculate system totals from node totals
        total_up = sum(node_data["up"] for node_data in node_totals.values())
        total_down = sum(node_data["down"] for node_data in node_totals.values())

        if not (total_up or total_down):
            logger.debug("No node usage to record")
            return

        # Update each node's uplink/downlink with concurrency control
        node_update_params = [
            {"node_id": node_id, "up": node_data["up"], "down": node_data["down"]}
            for node_id, node_data in sorted(node_totals.items())
            if node_data["up"] or node_data["down"]
        ]

        if node_update_params:
            node_update_stmt = (
                update(Node)
                .where(Node.id == bindparam("node_id"))
                .values(uplink=Node.uplink + bindparam("up"), downlink=Node.downlink + bindparam("down"))
                .execution_options(synchronize_session=False)
            )
            async with JOB_SEM:
                await safe_execute(node_update_stmt, node_update_params)
            logger.debug(f"Updated {len(node_update_params)} nodes")

        # Update system totals with concurrency control
        system_update_stmt = update(System).values(
            uplink=System.uplink + total_up, downlink=System.downlink + total_down
        )
        async with JOB_SEM:
            await safe_execute(system_update_stmt)

        if usage_settings.disable_recording_node_usage:
            return

        # Batch all node usage writes
        await record_node_stats_batched(api_params)

        await after_record_node_usages(nodes, _get_time_bucket())

        job_duration = time.time() - job_start_time
        logger.info(
            f"Node usage recording completed in {job_duration:.2f}s: "
            f"{len(node_update_params)} nodes, total: {total_up + total_down} bytes"
        )

    except Exception:
        job_duration = time.time() - job_start_time
        logger.exception(f"Node usage recording failed after {job_duration:.2f}s")
        raise


async def record_node_usages():
    """Record node usages. Same skip rules as ``record_user_usages``."""
    global _node_usage_running
    if _node_usage_running:
        logger.warning(
            "record_node_usages skipped; previous run still in progress. %s",
            _usage_job_hint("JOB_RECORD_NODE_USAGES_INTERVAL", job_settings.record_node_usages_interval),
        )
        return

    _node_usage_running = True
    try:
        await _await_usage_job(
            "record_node_usages",
            _record_node_usages_impl,
            job_settings.record_node_usages_interval,
            "JOB_RECORD_NODE_USAGES_INTERVAL",
        )
    finally:
        _node_usage_running = False


if runtime_settings.role.runs_node:
    scheduler.add_job(
        record_user_usages,
        "interval",
        seconds=job_settings.record_user_usages_interval,
        start_date=dt.now(UTC) + td(seconds=30),
        coalesce=True,
        max_instances=1,
        id="record_user_usages",
        replace_existing=True,
    )

    scheduler.add_job(
        record_node_usages,
        "interval",
        seconds=job_settings.record_node_usages_interval,
        start_date=dt.now(UTC) + td(seconds=15),
        coalesce=True,
        max_instances=1,
        id="record_node_usages",
        replace_existing=True,
    )
