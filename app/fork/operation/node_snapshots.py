import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator

from PasarGuardNodeBridge import NodeAPIError, PasarGuardNode
from PasarGuardNodeBridge.common.service_pb2 import StatType, User as ProtoUser
from PasarGuardNodeBridge.storage import get_default_user_sync_store

from app.utils.logger import get_logger

logger = get_logger("node-operation")

USAGE_DRAIN_TIMEOUT = 5
OWN_FENCE = object()
NO_ATTACH_PUSH = object()


class CoreUsersSnapshots:
    def __init__(self, reader):
        self._reader = reader
        self._clock = itertools.count()
        self._locks: dict[tuple, asyncio.Lock] = {}
        self._latest: dict[tuple, tuple[int, list]] = {}

    def loader(self, core_ids: tuple[int, ...], cores: list) -> CoreUsersLoader:
        return CoreUsersLoader(self, core_ids, cores)

    async def read(self, core_ids: tuple[int, ...], cores: list) -> list:
        arrived = next(self._clock)
        async with self._locks.setdefault(core_ids, asyncio.Lock()):
            latest = self._latest.get(core_ids)
            if latest is not None and latest[0] > arrived:
                return latest[1]
            started = next(self._clock)
            tags: list[str] = []
            protocols: set = set()
            for core in cores:
                tags.extend(core.inbounds)
                protocols.update(core.protocols or ())
            users = await self._reader(tags, frozenset(protocols))
            self._latest[core_ids] = (started, users)
            return users


class CoreUsersLoader:
    def __init__(self, snapshots: CoreUsersSnapshots, core_ids: tuple[int, ...], cores: list):
        self.snapshots = snapshots
        self.core_ids = core_ids
        self.cores = cores

    async def __call__(self) -> list:
        return await self.snapshots.read(self.core_ids, self.cores)


def _merge_users(user_lists: list[list]) -> list:
    merged: dict[str, ProtoUser] = {}
    for users in user_lists:
        for user in users:
            current = merged.get(user.email)
            if current is None:
                merged[user.email] = ProtoUser()
                merged[user.email].CopyFrom(user)
                continue
            current.proxies.MergeFrom(user.proxies)
            current.inbounds.extend(tag for tag in user.inbounds if tag not in current.inbounds)
    return list(merged.values())


def node_users_loader(users, extra_cores: list[tuple] | None):
    extras = [extra_users for _, extra_core, extra_users in extra_cores or [] if extra_core is not None]
    if not extras:
        return users
    parts = [users, *extras]
    if all(isinstance(part, CoreUsersLoader) and part.snapshots is users.snapshots for part in parts):
        core_ids = tuple(core_id for part in parts for core_id in part.core_ids)
        return users.snapshots.loader(core_ids, [core for part in parts for core in part.cores])

    async def load() -> list:
        return _merge_users([await resolve_users(part) for part in parts])

    return load


async def resolve_users(users) -> list:
    if callable(users):
        return await users()
    return users


@contextlib.asynccontextmanager
async def full_sync_fence(pg_node: PasarGuardNode, wait: float = 0) -> AsyncIterator[object | None]:
    fence = getattr(pg_node, "full_sync_fence", None)
    if not callable(fence):
        yield None
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    delay = 0.2
    async with contextlib.AsyncExitStack() as stack:
        while True:
            try:
                held = await stack.enter_async_context(fence())
                break
            except NodeAPIError as exc:
                if exc.code != 409 or loop.time() + delay > deadline:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 2.0)
        yield held


def check_fence(held) -> None:
    if held is not None:
        held.check()


async def drain_node_usage(pg_node: PasarGuardNode, node_id: int) -> None:
    get_stats = getattr(pg_node, "get_stats", None)
    if not callable(get_stats):
        return
    from app.jobs import record_usages

    try:
        epoch_at_poll = await record_usages.current_usage_epochs()
    except Exception as exc:
        logger.warning(f"Not draining node {node_id} before restarting it: usage epochs are unreadable: {exc}")
        return
    try:
        response = await get_stats(stat_type=StatType.UsersStat, reset=True, timeout=USAGE_DRAIN_TIMEOUT)
    except Exception as exc:
        logger.debug(f"Node {node_id} had no counters to drain before its restart: {exc}")
        return
    stats, _ = record_usages._process_users_stats_response(response)
    if not stats:
        return
    coefficient = await record_usages._node_usage_coefficient(pg_node, node_id)
    record_usages._retained_cohorts.append(
        record_usages.UsageCohort({node_id: stats}, {node_id: coefficient}, epoch_at_poll)
    )
    logger.info(
        f"Drained {record_usages._raw_bytes({node_id: stats})} raw bytes from node {node_id} before restarting its "
        "core; they are billed on the next usage cycle"
    )


async def flush_drained_usage() -> None:
    from app.jobs import record_usages

    if record_usages._retained_cohorts:
        await record_usages._flush_retained_cohorts()


async def clear_local_user_queue(node_id: int) -> None:
    from app.node.nats_memory import get_bridge_memory

    store, _, _ = get_bridge_memory()
    if store is None:
        await get_default_user_sync_store().clear(str(node_id))
