import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import ClassVar

import nats.errors
from fastapi import HTTPException
from PasarGuardNodeBridge import Health, NodeAPIError, PasarGuardNode
from PasarGuardNodeBridge.common import service_pb2 as service
from PasarGuardNodeBridge.storage import LifecycleStatus
from sqlalchemy.exc import IntegrityError

from app import notification
from app.core.manager import core_manager
from app.db import AsyncSession, GetDB
from app.db.crud.node import (
    bulk_reset_node_usage,
    bulk_update_node_status,
    clear_usage_data,
    create_node,
    get_node_by_id,
    get_node_stats,
    get_nodes,
    get_nodes_outbound_usage,
    get_nodes_simple,
    get_nodes_usage,
    modify_node,
    reorder_nodes,
    reset_node_usage,
    update_node_status,
)
from app.db.crud.user import get_user_by_id, get_user_count_metric_stats
from app.db.models import Node, NodeStatus
from app.fork.cores.singbox_guard import singbox_index_keyed_inbounds, unsafe_singbox_node_message
from app.fork.crud.node import CoreConfigMissing
from app.fork.node_error_status import node_error_status
from app.fork.operation.node_connect_jobs import run_withdrawable_connect
from app.fork.operation.node_extras import (
    _BACKEND_TYPE_BY_CORE,  # noqa: F401
    _CONNECT_LOCKS,  # noqa: F401
    _MULTI_INSTANCE_BACKENDS,  # noqa: F401
    L2TP_MIN_NODE_VERSION,  # noqa: F401
    NodeExtraCoresMixin,
    _connect_lock,
    _known_node_version,
    _l2tp_unsupported_message,
    _node_lacks_l2tp,
    connect_snapshot,
    connect_snapshot_superseded,
    current_connect_snapshot,
    forget_connect_pushes,
    note_ambiguous_start,
    note_connect_push,
    with_connect_snapshot,
)
from app.fork.operation.node_retirement import Retirement, refusal_message, retire_nodes
from app.fork.operation.node_snapshots import (
    NO_ATTACH_PUSH,
    OWN_FENCE,
    CoreUsersSnapshots,
    check_fence,
    clear_local_user_queue,
    drain_node_usage,
    full_sync_fence,
    node_users_loader,
    resolve_users,
)
from app.fork.operation.usage_guard import usage_aggregation_guard
from app.models.admin import AdminDetails
from app.models.core import CoreType
from app.models.node import (
    BulkNodesActionResponse,
    BulkNodeSelection,
    NodeClearUsageQuery,
    NodeCoreUpdate,
    NodeCreate,
    NodeGeoFilesUpdate,
    NodeListQuery,
    NodeModify,
    NodeNotification,
    NodeOutboundUsageQuery,
    NodeResponse,
    NodeSimple,
    NodeSimpleListQuery,
    NodesResponse,
    NodesSimpleResponse,
    NodeStatsPeriodQuery,
    NodeUsageQuery,
    RemoveNodesResponse,
    UsageTable,
    UserIPList,
    UserIPListAll,
)
from app.models.stats import (
    NodeOutboundsLatencyResponse,
    NodeOutboundUsageStatsList,
    NodeRealtimeStats,
    NodeStatsList,
    NodeUsageStatsList,
    UserCountMetric,
    UserCountMetricStatsList,
    validate_user_count_metric_scope,
)
from app.nats import needs_shared_bridge_memory
from app.nats.leader import needs_job_leader
from app.nats.node_rpc import node_nats_client
from app.node import UserSyncPreemptedError, core_users, node_manager
from app.node.bridge import FullSyncConflictError
from app.node.heartbeat import heartbeat_interval, keep_alive_too_short, shortest_safe_keep_alive
from app.node.manager_sync import publish_node_sync
from app.node.nats_memory import clear_bridge_memory_for_node
from app.operation import BaseOperation, OperatorType
from app.utils.http_client import INVALID_PROXY_MESSAGE, InvalidProxyURL
from app.utils.logger import get_logger
from config import nats_settings, runtime_settings

MAX_MESSAGE_LENGTH = 128
# Cap parallel start/attach so ~100 nodes don't stampede NATS lifecycle KV.
CONNECT_CONCURRENCY = 10
ATTACH_SYNC_ATTEMPTS = 3
ATTACH_SYNC_BACKOFF = 1.0
FENCE_WAIT_SECONDS = 60.0
WIREGUARD_BATCHED_START_THRESHOLD = 1000
WIREGUARD_START_BATCH_SIZE = 100
type CoreUsers = list | Callable[[], Awaitable[list]]

logger = get_logger("node-operation")


# Maintenance RPCs (split roles) wait for the node worker, which waits for node-serviced's pg-node command
# (5 minute deadline; the bridge itself allows 330 s). The default node RPC timeout (30 s) is far too short.
_MAINTENANCE_RPC_TIMEOUT = 340.0


def _maintenance_rpc_timeout() -> float:
    return max(_MAINTENANCE_RPC_TIMEOUT, nats_settings.node_rpc_timeout)


_BRIDGE_TIMEOUT_CODE = -1


def _maintenance_error_status(error: NodeAPIError) -> int:
    if error.code > 0:
        return node_error_status(error.code)
    if error.code == _BRIDGE_TIMEOUT_CODE or isinstance(error.__cause__, TimeoutError):
        return 504
    return 503


class NodeOperation(NodeExtraCoresMixin, BaseOperation):
    # Local Start RPCs in progress on this process. Health checks must not fire a
    # second Start just because pg-node still returns "core is not started yet".
    _in_flight_connects: ClassVar[set[int]] = set()
    _pending_user_sync: ClassVar[set[int]] = set()

    def __init__(self, operator_type: OperatorType):
        """Select local or remote node-operation handlers for this process role."""
        super().__init__(operator_type)
        self._retires_here = runtime_settings.role.runs_node
        if runtime_settings.role.runs_node:
            sync = needs_shared_bridge_memory()
            self._update_node_impl = self._update_node_sync if sync else self._update_node_local
            self._remove_node_impl = self._remove_node_sync if sync else self._remove_node_local
            self._connect_single_impl = self._connect_single_node_sync if sync else self._connect_single_node_local
            self._connect_bulk_impl = self._connect_nodes_bulk_sync if sync else self._connect_nodes_bulk_local
            self._disconnect_single_impl = (
                self._disconnect_single_node_sync if sync else self._disconnect_single_node_local
            )

            self._sync_node_users_impl = self._sync_node_users_local
            self._get_node_stats_impl = self._get_node_system_stats_local
            self._get_nodes_stats_impl = self._get_nodes_system_stats_local
            self._get_outbounds_latency_impl = self._get_outbounds_latency_local
            self._get_user_online_stats_impl = self._get_user_online_stats_local
            self._get_user_ip_list_impl = self._get_user_ip_list_local
            self._get_user_ip_list_all_impl = self._get_user_ip_list_all_local
            self._update_node_api_impl = self._update_node_api_local
            self._update_core_impl = self._update_core_local
            self._update_geofiles_impl = self._update_geofiles_local
            self._get_logs_impl = self._get_logs_local
            self._restart_all_impl = self._restart_all_nodes_local
        else:
            self._update_node_impl = self._update_node_remote
            self._remove_node_impl = self._remove_node_remote
            self._connect_single_impl = self._connect_single_node_remote
            self._connect_bulk_impl = self._connect_nodes_bulk_remote
            self._disconnect_single_impl = self._disconnect_single_node_remote
            self._sync_node_users_impl = self._sync_node_users_remote
            self._get_node_stats_impl = self._get_node_system_stats_remote
            self._get_nodes_stats_impl = self._get_nodes_system_stats_remote
            self._get_outbounds_latency_impl = self._get_outbounds_latency_remote
            self._get_user_online_stats_impl = self._get_user_online_stats_remote
            self._get_user_ip_list_impl = self._get_user_ip_list_remote
            self._get_user_ip_list_all_impl = self._get_user_ip_list_all_remote
            self._update_node_api_impl = self._update_node_api_remote
            self._update_core_impl = self._update_core_remote
            self._update_geofiles_impl = self._update_geofiles_remote
            self._get_logs_impl = self._get_logs_remote
            self._restart_all_impl = self._restart_all_nodes_remote

    async def reorder_nodes(self, db: AsyncSession, ordered_ids: list[int], admin: AdminDetails) -> None:
        """Persist a node order requested by an administrator."""
        if not await reorder_nodes(db, ordered_ids):
            await self.raise_error(message="Node not found", code=404, db=db)
        logger.info('Nodes reordered by admin "%s"', admin.username)

    async def get_db_nodes(
        self,
        db: AsyncSession,
        query: NodeListQuery,
    ) -> NodesResponse:
        db_nodes, count = await get_nodes(
            db=db,
            query=query,
            load_usage_logs=False,
            load_lifetime_usage=True,
        )
        node_responses = [NodeResponse.model_validate(node) for node in db_nodes]
        return NodesResponse(nodes=node_responses, total=count)

    async def get_nodes_simple(
        self,
        db: AsyncSession,
        query: NodeSimpleListQuery,
    ) -> NodesSimpleResponse:
        """Get lightweight node list with only id and name"""
        rows, total = await get_nodes_simple(db=db, query=query)

        nodes = [NodeSimple(id=row[0], name=row[1], status=row[2], core_config_id=row[3]) for row in rows]

        return NodesSimpleResponse(nodes=nodes, total=total)

    @staticmethod
    async def _update_single_node_status(
        db: AsyncSession,
        node_id: int,
        status: NodeStatus,
        message: str = "",
        xray_version: str = "",
        node_version: str = "",
        send_notification: bool = True,
        expected_status: NodeStatus | None = None,
    ) -> bool:
        """
        Update single node status with optional notification.

        Args:
            db (AsyncSession): Database session to use.
            node_id (int): ID of the node to update.
            status (NodeStatus): New status.
            message (str): Status message (e.g., error details).
            xray_version (str): Xray version.
            node_version (str): Node version.
            send_notification (bool): Whether to send notification.
            expected_status: Ignore observations made before a concurrent status change.
        """
        db_node = await get_node_by_id(db, node_id, load_usage_logs=False)
        if not db_node:
            return False
        if expected_status is not None and db_node.status != expected_status:
            return False

        if db_node.status in (NodeStatus.disabled, NodeStatus.limited) and status not in (
            NodeStatus.disabled,
            NodeStatus.limited,
        ):
            return False

        old_status = db_node.status

        if expected_status is not None and old_status == status and db_node.message == message:
            return True

        if status == NodeStatus.error:
            if expected_status is not None:
                xray_version, node_version = db_node.xray_version, db_node.node_version
            logger.error(f"Failed to connect node {db_node.name} with id {db_node.id}, Error: {message}")

        updated = await update_node_status(
            db=db,
            db_node=db_node,
            status=status,
            message=message,
            xray_version=xray_version,
            node_version=node_version,
            expected_status=expected_status,
        )

        if updated is None or db_node.status != status:
            return False
        if not send_notification:
            return True

        if status == NodeStatus.connected and old_status != NodeStatus.connected:
            node_notif = NodeNotification(
                id=db_node.id,
                name=db_node.name,
                xray_version=xray_version,
                node_version=node_version,
            )
            asyncio.create_task(notification.connect_node(node_notif))
        elif status == NodeStatus.error and old_status != NodeStatus.error:
            truncated_message = (
                message[: MAX_MESSAGE_LENGTH - 3] + "..." if len(message) > MAX_MESSAGE_LENGTH else message
            )
            node_notif = NodeNotification(
                id=db_node.id,
                name=db_node.name,
                message=truncated_message,
            )
            asyncio.create_task(notification.error_node(node_notif))

        return True

    @staticmethod
    async def _register_if_wanted(db_node: Node) -> bool:
        async with _connect_lock(db_node.id):
            async with GetDB() as fresh:
                current = await get_node_by_id(fresh, db_node.id, load_usage_logs=False)
            if current is None or current.status in (NodeStatus.disabled, NodeStatus.limited):
                logger.info(f'Not connecting "{db_node.name}" node: it was disabled, limited or removed meanwhile')
                return False
            await node_manager.update_node(current)
            return True

    @staticmethod
    async def _get_core_users_map(
        db: AsyncSession, core_ids: set[int], *, lazy: bool = False
    ) -> tuple[dict[int, object | None], dict[int, CoreUsers]]:
        if not core_ids:
            return {}, {}

        resolved_cores = await core_manager.get_cores(core_ids | {1})
        default_core = resolved_cores.get(1)
        cores_by_id: dict[int, object | None] = {}
        users_by_core: dict[int, CoreUsers] = {}

        async def read_in_fresh_session(inbound_tags, allowed_protocols):
            async with AsyncSession(bind=db.bind, expire_on_commit=False) as session:
                return await core_users(db=session, inbound_tags=inbound_tags, allowed_protocols=allowed_protocols)

        snapshots = CoreUsersSnapshots(read_in_fresh_session)

        for core_id in core_ids:
            core = resolved_cores.get(core_id) or default_core
            cores_by_id[core_id] = core
            if core is None:
                users_by_core[core_id] = []
                continue

            if lazy:
                users_by_core[core_id] = snapshots.loader((core_id,), [core])
            else:
                users_by_core[core_id] = await core_users(
                    db=db, inbound_tags=core.inbounds, allowed_protocols=core.protocols
                )

        return cores_by_id, users_by_core

    @staticmethod
    async def _resume_shared_sync(pg_node: PasarGuardNode):
        if needs_shared_bridge_memory():
            pg_node._work_available.set()
            await pg_node._ensure_sync_worker_running()

    @staticmethod
    async def _attach_if_running(pg_node: PasarGuardNode, node_name: str):
        """Attach to an already-started remote core without calling Start RPC."""
        try:
            state = await pg_node.get_lifecycle_state()
            if (
                state is not None
                and state.observed not in (LifecycleStatus.HEALTHY, LifecycleStatus.STARTING)
                and state.desired is not LifecycleStatus.HEALTHY
            ):
                return None

            info = await pg_node.info()
            if info is None or not info.started or not info.node_version or not info.core_version:
                return None

            # The running flag can outlive a missing WireGuard interface. Confirm
            # a broken backend has recovered before marking the attachment healthy.
            if await pg_node.get_health() is Health.BROKEN or (
                state is not None and state.observed is LifecycleStatus.BROKEN
            ):
                await pg_node.get_backend_stats()

            await pg_node.connect(info.node_version, info.core_version)
            # Shared pending work survives a panel restart. Attaching must
            # wake the lazy bridge worker even without a fresh user update.
            await NodeOperation._resume_shared_sync(pg_node)
            if state is not None:
                await pg_node.update_observed_lifecycle(LifecycleStatus.HEALTHY, expected_epoch=state.epoch)
            logger.debug(
                f'Attached to already-running "{node_name}" node v{info.node_version}, core v{info.core_version}'
            )
            return info
        except Exception as exc:
            logger.debug(f'Attach skipped for "{node_name}": {exc}')
            return None

    @staticmethod
    async def _push_users_after_attach(pg_node: PasarGuardNode, db_node: Node, users: CoreUsers, held=None) -> bool:
        delay = ATTACH_SYNC_BACKOFF
        for attempt in range(ATTACH_SYNC_ATTEMPTS):
            try:
                users = await resolve_users(users)
                check_fence(held)
                await pg_node.sync_users(users, flush_pending=False)
                NodeOperation._pending_user_sync.discard(db_node.id)
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt + 1 == ATTACH_SYNC_ATTEMPTS:
                    NodeOperation._pending_user_sync.add(db_node.id)
                    logger.error(
                        f'Could not refresh the user list on the already-running "{db_node.name}" node after '
                        f"{ATTACH_SYNC_ATTEMPTS} attempts: {exc}. Clients will be rejected until it succeeds; "
                        "the next reconnect will retry it."
                    )
                    return False
                await asyncio.sleep(delay)
                delay *= 2
        return False

    @staticmethod
    async def _start_or_attach_node(
        pg_node: PasarGuardNode,
        db_node: Node,
        core,
        users: CoreUsers,
        backend_type,
        *,
        force_start: bool = False,
        stop_first: bool = True,
        attach_users: CoreUsers | None = None,
        held=OWN_FENCE,
    ):
        if held is not OWN_FENCE:
            return await NodeOperation._start_or_attach_fenced(
                pg_node, db_node, core, users, backend_type, force_start, attach_users, stop_first, held
            )
        async with full_sync_fence(pg_node) as own:
            return await NodeOperation._start_or_attach_fenced(
                pg_node, db_node, core, users, backend_type, force_start, attach_users, stop_first, own
            )

    @staticmethod
    async def _start_or_attach_fenced(
        pg_node: PasarGuardNode,
        db_node: Node,
        core,
        users: CoreUsers,
        backend_type,
        force_start: bool,
        attach_users: CoreUsers | None,
        stop_first: bool,
        held,
    ):
        needs_stop = force_start
        if not force_start:
            state = await pg_node.get_lifecycle_state()
            if state is not None and (
                state.observed in (LifecycleStatus.HEALTHY, LifecycleStatus.STARTING)
                or state.desired is LifecycleStatus.HEALTHY
            ):
                attached = await NodeOperation._attach_if_running(pg_node, db_node.name)
                if attached is not None and (
                    attach_users is NO_ATTACH_PUSH
                    or await NodeOperation._push_users_after_attach(
                        pg_node, db_node, users if attach_users is None else attach_users, held
                    )
                ):
                    return attached
                if state.observed is LifecycleStatus.STARTING:
                    # Another worker is already starting this node right now - don't race
                    # it for the lease (that's a guaranteed 409 plus wasted KV round-trips).
                    # Skip; the next retry cycle will check again once it's done.
                    return
                if attached is not None:
                    needs_stop = True
                    logger.warning(f'Restarting "{db_node.name}" because its user list could not be refreshed')

        users = await resolve_users(users)
        check_fence(held)
        await drain_node_usage(pg_node, db_node.id)
        # Starting WireGuard with thousands of peers makes the lifecycle RPC exceed its
        # short timeout. Bring up the interface first, then apply peers through the
        # node's bounded delta-sync path below.
        batch_wireguard_users = (
            backend_type == service.BackendType.WIREGUARD and len(users) >= WIREGUARD_BATCHED_START_THRESHOLD
        )

        if needs_stop and stop_first:
            try:
                await pg_node.stop()
            except Exception as exc:
                if not force_start:
                    logger.error(
                        f'Refusing to start "{db_node.name}": its running core could not be stopped, '
                        f"and starting a second one would fight the first: {exc}"
                    )
                    raise
                logger.debug(f'Stop before force start of "{db_node.name}" skipped: {exc}')

        check_fence(held)
        start_kwargs = {
            "config": core.to_str(),
            "backend_type": backend_type,
            "users": [] if batch_wireguard_users else users,
            "keep_alive": db_node.keep_alive,
        }
        if core.type == CoreType.xray:
            start_kwargs["exclude_inbounds"] = core.exclude_inbound_tags

        log = logger.info if force_start else logger.debug
        log(f'Starting "{db_node.name}" node')
        try:
            info = await pg_node.start(**start_kwargs)
        except asyncio.CancelledError:
            if batch_wireguard_users:
                await NodeOperation._cleanup_cancelled_batched_start(db_node.id, pg_node)
            raise
        if batch_wireguard_users:
            try:
                synced_node = await node_manager.sync_users_batched(
                    db_node.id, pg_node, users, batch_size=WIREGUARD_START_BATCH_SIZE
                )
                if synced_node is None:
                    raise RuntimeError("node connection changed during initial user sync")
                check_fence(held)
            except asyncio.CancelledError:
                await NodeOperation._cleanup_cancelled_batched_start(db_node.id, pg_node)
                raise
            except UserSyncPreemptedError as exc:
                # A connection change or removal of the node won. That operation decides
                # whether the old backend is stopped and reconnects the node itself, so this
                # start neither stops it nor reports an error (-4 makes connect_node skip).
                raise NodeAPIError(-4, str(exc)) from exc
            except Exception as exc:
                try:
                    await node_manager.stop_node_if_current(db_node.id, pg_node)
                except Exception:
                    pass
                raise NodeAPIError(500, f"Failed to sync users after starting WireGuard: {exc}") from exc
        return info

    @staticmethod
    async def _cleanup_cancelled_batched_start(node_id: int, pg_node: PasarGuardNode) -> None:
        cleanup_task = asyncio.create_task(node_manager.stop_node_if_current(node_id, pg_node))
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            # Keep waiting if shutdown requests cancellation again while the
            # backend is being returned to a consistent stopped state.
            try:
                await cleanup_task
            except Exception:
                pass
        except Exception:
            pass

    @staticmethod
    async def connect_node(
        db_node: Node,
        core,
        users: CoreUsers,
        extra_cores: list[tuple] | None = None,
        *,
        force_start: bool = False,
        stop_first: bool = True,
        push_on_attach: bool = True,
    ) -> dict | None:
        """
        Connect to a node and return status result (does NOT update database).

        Args:
            db_node (Node): Node object from database.
            core: Pre-fetched core config for this node.
            users: Core users or a deferred snapshot loader, used only for Start.
            extra_cores: Additional cores to run alongside the primary one.
            force_start: Push a new Start RPC (and re-add running extra backends) even when
                the node looks healthy, so a changed config actually reaches the daemon.
            stop_first: With force_start, stop the core before Start. False lets the node apply
                a changed config to the running core when it can.

        Returns:
            dict: {node_id, status, message, xray_version, node_version, old_status}
            None: if connection should be skipped
        """
        snapshot = current_connect_snapshot()

        async def connect_under_the_lock() -> dict | None:
            if connect_snapshot_superseded(db_node.id, snapshot):
                logger.debug(f'Dropping a stale connect of "{db_node.name}" node, a newer config already reached it')
                return None
            return await NodeOperation._connect_node_inner(
                db_node, core, users, extra_cores, force_start, stop_first, push_on_attach, snapshot
            )

        return await run_withdrawable_connect(db_node.id, connect_under_the_lock)

    @staticmethod
    async def _connect_node_inner(
        db_node: Node,
        core,
        users: CoreUsers,
        extra_cores: list[tuple] | None,
        force_start: bool,
        stop_first: bool = True,
        push_on_attach: bool = True,
        snapshot: int = 0,
    ) -> dict | None:
        pg_node: PasarGuardNode | None = await node_manager.get_node(db_node.id)
        if pg_node is None:
            return None
        if core is None:
            return None

        old_status = db_node.status
        if not force_start:
            try:
                if await pg_node.get_health() == Health.HEALTHY:
                    if old_status == NodeStatus.connected:
                        return None
                    node_version, core_version = await pg_node.get_versions()
                    return {
                        "node_id": db_node.id,
                        "status": NodeStatus.connected,
                        "message": "",
                        "xray_version": core_version,
                        "node_version": node_version,
                        "old_status": old_status,
                    }
            except Exception:
                pass

        logger.info(f'Connecting to "{db_node.name}" node')
        if core.type == CoreType.wg:
            type = service.BackendType.WIREGUARD
        elif core.type == CoreType.singbox:
            type = service.BackendType.SING_BOX
        elif core.type == CoreType.openvpn:
            type = service.BackendType.OPEN_VPN
        elif core.type == CoreType.l2tp:
            type = service.BackendType.L2TP
        else:
            type = service.BackendType.XRAY

        if core.type == CoreType.l2tp:
            known_version = await _known_node_version(pg_node)
            if _node_lacks_l2tp(known_version):
                message = _l2tp_unsupported_message(known_version)
                logger.error(f'Refusing to start an L2TP core on "{db_node.name}": {message}')
                return {
                    "node_id": db_node.id,
                    "status": NodeStatus.error,
                    "message": message,
                    "xray_version": "",
                    "node_version": known_version,
                    "old_status": old_status,
                }

        singbox_offenders = singbox_index_keyed_inbounds(core)
        if singbox_offenders:
            known_version = await _known_node_version(pg_node)
            message = unsafe_singbox_node_message("This node's sing-box core", singbox_offenders, known_version)
            if message:
                logger.error(f'Refusing to start a sing-box core on "{db_node.name}": {message}')
                return {
                    "node_id": db_node.id,
                    "status": NodeStatus.error,
                    "message": message,
                    "xray_version": "",
                    "node_version": known_version,
                    "old_status": old_status,
                }
        note_connect_push(db_node.id, snapshot)
        NodeOperation._in_flight_connects.add(db_node.id)

        try:
            async with full_sync_fence(pg_node, wait=FENCE_WAIT_SECONDS) as held:
                return await NodeOperation._connect_fenced(
                    pg_node,
                    db_node,
                    core,
                    users,
                    extra_cores,
                    type,
                    force_start,
                    stop_first,
                    old_status,
                    held,
                    push_on_attach,
                )
        except NodeAPIError as e:
            if e.code != 409:
                raise
            logger.error(f'Could not connect "{db_node.name}" node: {e.detail}')
            return {
                "node_id": db_node.id,
                "status": NodeStatus.error,
                "message": f"Not started: {e.detail}"[:1024],
                "xray_version": "",
                "node_version": "",
                "old_status": old_status,
            }
        finally:
            NodeOperation._in_flight_connects.discard(db_node.id)

    @staticmethod
    async def _connect_fenced(
        pg_node: PasarGuardNode,
        db_node: Node,
        core,
        users: CoreUsers,
        extra_cores: list[tuple] | None,
        type,
        force_start: bool,
        stop_first: bool,
        old_status: NodeStatus,
        held,
        push_on_attach: bool = True,
    ) -> dict | None:
        attach_users = node_users_loader(users, extra_cores) if push_on_attach else NO_ATTACH_PUSH
        try:
            info = await NodeOperation._start_or_attach_node(
                pg_node,
                db_node,
                core,
                users,
                type,
                force_start=force_start,
                stop_first=stop_first,
                attach_users=attach_users,
                held=held,
            )
            if info is None:
                return None

            log = logger.info if force_start or old_status != NodeStatus.connected else logger.debug
            log(f'Connected to "{db_node.name}" node v{info.node_version}, core run on v{info.core_version}')

            failed_extras = "; ".join(
                p
                for p in (
                    await NodeOperation._remove_surplus_backends(pg_node, db_node, extra_cores, core.type),
                    await NodeOperation._add_extra_cores(
                        pg_node, db_node, extra_cores, restart_running=force_start, held=held
                    ),
                )
                if p
            )[:1024]

            return {
                "node_id": db_node.id,
                "status": NodeStatus.connected,
                "message": failed_extras,
                "xray_version": info.core_version,
                "node_version": info.node_version,
                "old_status": old_status,
            }
        except NodeAPIError as e:
            if e.code == -4:
                return None
            if e.code == 409:
                # Another worker holds the lifecycle lease; try attach once more.
                attached = await NodeOperation._attach_if_running(pg_node, db_node.name)
                if attached is not None:
                    return {
                        "node_id": db_node.id,
                        "status": NodeStatus.connected,
                        "message": "; ".join(
                            p
                            for p in (
                                await NodeOperation._remove_surplus_backends(
                                    pg_node, db_node, extra_cores, core.type
                                ),
                                await NodeOperation._add_extra_cores(
                                    pg_node, db_node, extra_cores, restart_running=force_start, held=held
                                ),
                            )
                            if p
                        )[:1024],
                        "xray_version": attached.core_version,
                        "node_version": attached.node_version,
                        "old_status": old_status,
                    }

                # A 409 only ever happens while another worker holds a live, unexpired
                # lifecycle lease (a dead worker's lease always expires via TTL/heartbeat),
                # so this is always legitimate concurrent work elsewhere - starting,
                # stopping, or updating the node's core/geofiles - not a fault with the
                # node itself. The attach above simply couldn't catch up mid-operation.
                # Skip silently instead of flagging the node as errored on every other
                # worker; the next retry cycle will reassess once that operation completes.
                logger.debug(f'"{db_node.name}" node lifecycle lease is held by another worker, will retry')
                return None

            if e.code == -1:
                note_ambiguous_start(db_node)
                # A timed-out Start has an ambiguous outcome: cancelling the panel-side
                # request does not guarantee that the remote node stopped starting. Probe
                # it before reporting an error so a late success is attached instead of
                # being torn down by the next health-check reconnect.
                attached = await NodeOperation._attach_if_running(pg_node, db_node.name)
                if attached is not None and (
                    attach_users is NO_ATTACH_PUSH
                    or await NodeOperation._push_users_after_attach(pg_node, db_node, attach_users, held)
                ):
                    logger.warning(
                        f'Attached to "{db_node.name}" after its Start request timed out and pushed the current '
                        "user list to the running core"
                    )
                    problems = [
                        await NodeOperation._remove_surplus_backends(pg_node, db_node, extra_cores, core.type),
                        await NodeOperation._add_extra_cores(pg_node, db_node, extra_cores, held=held),
                    ]
                    if force_start:
                        problems.insert(
                            0,
                            "the restart timed out and the panel attached to the core that was already running, "
                            "so its configuration may predate this restart",
                        )
                    return {
                        "node_id": db_node.id,
                        "status": NodeStatus.connected,
                        "message": "; ".join(p for p in problems if p)[:1024],
                        "xray_version": attached.core_version,
                        "node_version": attached.node_version,
                        "old_status": old_status,
                    }

            detail = e.detail[:1020] + "..." if len(e.detail) > 1024 else e.detail
            if core.type == CoreType.l2tp and "invalid backend type" in detail.lower():
                detail = _l2tp_unsupported_message(await _known_node_version(pg_node))

            logger.error(f"Failed to connect node {db_node.name} with id {db_node.id}, Error: {detail}")

            return {
                "node_id": db_node.id,
                "status": NodeStatus.error,
                "message": detail,
                "xray_version": "",
                "node_version": "",
                "old_status": old_status,
            }

    async def _connect_single_node_background(self, node_id: int, *, force_start: bool = False) -> None:
        try:
            async with GetDB() as db:
                await self._connect_single_impl(db, node_id, force_start=force_start)
        except Exception as exc:
            logger.error(f"Background node connection failed for node {node_id}: {exc}")

    @staticmethod
    def _warn_short_keep_alive(node_name: str, keep_alive: int) -> None:
        # Values that work today stay valid; this only explains a stopped core later.
        if keep_alive_too_short(keep_alive):
            logger.warning(
                'Node "%s" keep_alive %ss is short: the panel sends a heartbeat every %ss, and %s stop its '
                "core. Use at least %ss (120-300s on unstable links), or 0 to never stop it.",
                node_name,
                keep_alive,
                f"{heartbeat_interval(keep_alive):g}",
                "two lost in a row or a job leader change" if needs_job_leader() else "two lost in a row",
                shortest_safe_keep_alive(),
            )

    async def create_node(self, db: AsyncSession, new_node: NodeCreate, admin: AdminDetails) -> NodeResponse:
        await self.get_validated_core_config(db, new_node.core_config_id)
        await self._validate_additional_cores(db, new_node.additional_core_config_ids, new_node.core_config_id)
        try:
            db_node = await create_node(db, new_node)
        except (IntegrityError, CoreConfigMissing):
            await self._refuse_node_write(
                db, new_node.name, [new_node.core_config_id, *(new_node.additional_core_config_ids or [])]
            )
        self._warn_short_keep_alive(db_node.name, db_node.keep_alive)

        try:
            await self._update_node_impl(db_node)
            asyncio.create_task(self._connect_single_node_background(db_node.id))
        except NodeAPIError as e:
            await self._update_single_node_status(db, db_node.id, NodeStatus.error, message=e.detail)
        except InvalidProxyURL:
            await self._update_single_node_status(db, db_node.id, NodeStatus.error, message=INVALID_PROXY_MESSAGE)

        logger.info(f'New node "{db_node.name}" with id "{db_node.id}" added by admin "{admin.username}"')

        node = NodeResponse.model_validate(db_node)
        asyncio.create_task(notification.create_node(node, admin.username))

        return node

    @staticmethod
    def _start_settings(db_node: Node) -> tuple:
        # A node without a core runs the default core; sending it is not a core change.
        return db_node.core_config_id or 1, tuple(db_node.additional_core_config_ids or ()), db_node.keep_alive

    async def modify_node(self, db: AsyncSession, node_id: int, modified_node: NodeModify, admin: AdminDetails) -> Node:
        db_node = await self.get_validated_node(db=db, node_id=node_id)
        stored_start_settings = self._start_settings(db_node)
        stored_keep_alive = db_node.keep_alive
        if modified_node.core_config_id is not None:
            await self.get_validated_core_config(db, modified_node.core_config_id)
        if modified_node.additional_core_config_ids is not None:
            await self._validate_additional_cores(
                db,
                modified_node.additional_core_config_ids,
                modified_node.core_config_id if modified_node.core_config_id is not None else db_node.core_config_id,
            )

        node_name = modified_node.name or db_node.name

        try:
            db_node = await modify_node(db, db_node, modified_node)
        except (IntegrityError, CoreConfigMissing):
            await self._refuse_node_write(
                db, node_name, [modified_node.core_config_id, *(modified_node.additional_core_config_ids or [])]
            )
        if db_node.keep_alive != stored_keep_alive:
            self._warn_short_keep_alive(db_node.name, db_node.keep_alive)

        if db_node.status in (NodeStatus.disabled, NodeStatus.limited):
            await self.disconnect_single_node(db_node.id)
        else:
            try:
                await self._update_node_impl(db_node)
                force_start = self._start_settings(db_node) != stored_start_settings
                asyncio.create_task(self._connect_single_node_background(db_node.id, force_start=force_start))
            except NodeAPIError as e:
                await self._update_single_node_status(db, db_node.id, NodeStatus.error, message=e.detail)
            except InvalidProxyURL:
                await self._update_single_node_status(db, db_node.id, NodeStatus.error, message=INVALID_PROXY_MESSAGE)

        logger.info(f'Node "{db_node.name}" with id "{db_node.id}" modified by admin "{admin.username}"')

        node = NodeResponse.model_validate(db_node)
        asyncio.create_task(notification.modify_node(node, admin.username))

        return node

    async def remove_node(self, db: AsyncSession, node_id: int, admin: AdminDetails) -> None:
        db_node: Node = await self.get_validated_node(db=db, node_id=node_id)
        node_response = NodeResponse.model_validate(db_node)

        results = await retire_nodes(db, [db_node], here=self._retires_here)
        refusal = refusal_message(results)
        if refusal:
            await self.raise_error(refusal, 503)
        if results[0].outcome is Retirement.absent:
            return

        logger.info(f'Node "{node_response.name}" with id "{node_response.id}" deleted by admin "{admin.username}"')

        asyncio.create_task(notification.remove_node(node_response, admin.username))

    async def reset_node_usage(self, db: AsyncSession, node_id: int, admin: AdminDetails) -> NodeResponse:
        """
        Reset a node's traffic usage (uplink and downlink to 0) and create a log entry.

        Args:
            db: Database session
            node_id: ID of the node to reset
            admin: Admin performing the action

        Returns:
            NodeResponse: Updated node object
        """
        db_node = await self.get_validated_node(db=db, node_id=node_id)
        was_limited = db_node.status == NodeStatus.limited

        # Store old values for notification
        old_uplink = db_node.uplink
        old_downlink = db_node.downlink

        # Reset usage (creates log entry and sets uplink/downlink to 0)
        db_node = await reset_node_usage(db, db_node)

        if was_limited:
            await self.connect_single_node(db, db_node.id)
            db_node = await self.get_validated_node(db=db, node_id=node_id)

        # Create response
        node = NodeResponse.model_validate(db_node)

        # Send notification
        asyncio.create_task(notification.reset_node_usage(node, admin.username, old_uplink, old_downlink))

        logger.info(f'Node "{db_node.name}" (ID: {db_node.id}) usage reset by admin "{admin.username}"')

        return node

    async def connect_nodes_bulk(
        self,
        db: AsyncSession,
        nodes: list[Node],
        *,
        force_start: bool = False,
        stop_first: bool = True,
    ) -> None:
        """
        Connect multiple nodes and bulk update their statuses.

        Args:
            db (AsyncSession): Database session.
            nodes (list[Node]): List of nodes to connect.
            force_start: Push a new Start RPC even to healthy nodes (config apply / restart).
            stop_first: With force_start, stop each core before Start.
        """
        await self._connect_bulk_impl(db, nodes, force_start=force_start, stop_first=stop_first)

    async def connect_single_node(
        self, db: AsyncSession, node_id: int, *, force_start: bool = False, health_check: bool = False
    ) -> None:
        """
        Connect a single node and update its status (optimized for single-node operations).

        Uses simple UPDATE statement instead of bulk update to avoid deadlock risks
        and unnecessary complexity.

        Args:
            db (AsyncSession): Database session.
            node_id (int): ID of the node to connect.
            force_start: Push a new Start RPC even when the node looks healthy.
            health_check: Defer automatic recovery status until probes confirm it.
        """
        if health_check:
            return await self.recover_node(db, node_id)
        return await self._connect_single_impl(db, node_id, force_start=force_start)

    async def _connect_single_node_remote(self, db: AsyncSession, node_id: int, *, force_start: bool = False) -> None:
        await node_nats_client.publish("connect_node", {"node_id": node_id, "force_start": force_start})

    async def disconnect_single_node(self, node_id: int) -> None:
        """
        Disconnect a single node from the node manager (stop it from running).

        Used when a node needs to be stopped (e.g., when limited or disabled).

        Args:
            node_id (int): ID of the node to disconnect.
        """
        await self._disconnect_single_impl(node_id)
        logger.info(f'Node "{node_id}" disconnected')

    async def restart_node(self, db: AsyncSession, node_id: int, admin: AdminDetails) -> None:
        await self.connect_single_node(db, node_id, force_start=True)
        logger.info(f'Node "{node_id}" restarted by admin "{admin.username}"')

    async def restart_all_node(
        self, db: AsyncSession, admin: AdminDetails, core_id: int | None = None, *, stop_first: bool = True
    ) -> None:
        await self._restart_all_impl(db, admin, core_id, stop_first=stop_first)
        logger.info(f'All nodes restarted by admin "{admin.username}"')

    async def get_usage(
        self,
        db: AsyncSession,
        query: NodeUsageQuery,
        admin: AdminDetails | None = None,
    ) -> NodeUsageStatsList:
        start, end = await self.validate_dates(query.start, query.end, True, period=query.period)
        async with usage_aggregation_guard(("nodes_usage", admin.id if admin else None)):
            return await get_nodes_usage(
                db,
                start,
                end,
                period=query.period,
                node_id=query.node_id,
                group_by_node=query.group_by_node,
            )

    async def get_outbound_usage(
        self,
        db: AsyncSession,
        query: NodeOutboundUsageQuery,
        admin: AdminDetails | None = None,
    ) -> NodeOutboundUsageStatsList:
        start, end = await self.validate_dates(query.start, query.end, True, period=query.period)
        async with usage_aggregation_guard(("nodes_outbound_usage", admin.id if admin else None)):
            return await get_nodes_outbound_usage(db, start, end, period=query.period, node_id=query.node_id)

    async def get_user_count_metric(
        self,
        db: AsyncSession,
        metric: UserCountMetric,
        query: NodeUsageQuery,
        admin: AdminDetails | None = None,
    ) -> UserCountMetricStatsList:
        start, end = await self.validate_dates(query.start, query.end, True, period=query.period)
        try:
            validate_user_count_metric_scope(metric, node_id=query.node_id, group_by_node=query.group_by_node)
        except ValueError as exc:
            await self.raise_error(message=str(exc), code=400)

        admins, allowed_group_ids = self._user_count_scope(admin) if admin is not None else (None, None)
        async with usage_aggregation_guard(("nodes_user_count", admin.id if admin else None)):
            return await get_user_count_metric_stats(
                db,
                admins=admins,
                allowed_group_ids=allowed_group_ids,
                start=start,
                end=end,
                period=query.period,
                metric=metric,
                node_id=query.node_id,
                group_by_node=query.group_by_node,
            )

    async def get_logs(self, node_id: int) -> Callable[[], AsyncIterator[asyncio.Queue]]:
        return await self._get_logs_impl(node_id)

    async def get_node_stats_periodic(
        self, db: AsyncSession, node_id: int, query: NodeStatsPeriodQuery
    ) -> NodeStatsList:
        start, end = await self.validate_dates(query.start, query.end, True, period=query.period)

        return await get_node_stats(db, node_id, start, end, period=query.period)

    async def get_node_system_stats(self, node_id: int) -> NodeRealtimeStats:
        return await self._get_node_stats_impl(node_id)

    async def get_nodes_system_stats(self) -> dict[int, NodeRealtimeStats | None]:
        return await self._get_nodes_stats_impl()

    async def get_outbounds_latency(
        self, node_id: int, name: str = "", timeout: int | None = None
    ) -> NodeOutboundsLatencyResponse:
        return await self._get_outbounds_latency_impl(node_id, name, timeout)

    async def _get_node_stats_safe(self, node_id: int) -> NodeRealtimeStats | None:
        """Wrapper method that returns None instead of raising exceptions"""
        try:
            return await self.get_node_system_stats(node_id)
        except Exception as e:
            logger.error(f"Error getting system stats for node {node_id}: {e}")
            return None

    async def get_user_online_stats_by_node(
        self, db: AsyncSession, node_id: int, user_id: int, admin: AdminDetails
    ) -> dict[int, int]:
        await self._validate_stats_user(db, user_id, admin)
        return await self._get_user_online_stats_impl(db, node_id, user_id)

    async def get_user_ip_list_by_node(
        self, db: AsyncSession, node_id: int, user_id: int, admin: AdminDetails
    ) -> UserIPList:
        await self._validate_stats_user(db, user_id, admin)
        return await self._get_user_ip_list_impl(db, node_id, user_id)

    async def get_user_ip_list_all_nodes(self, db: AsyncSession, user_id: int, admin: AdminDetails) -> UserIPListAll:
        await self._validate_stats_user(db, user_id, admin)
        return await self._get_user_ip_list_all_impl(db, user_id)

    async def _get_node_user_ip_list_safe(self, node_id: int, email: str) -> dict[str, int] | None:
        """Wrapper method that returns None instead of raising exceptions"""
        try:
            node = await node_manager.get_node(node_id)
            if node is None:
                return None

            stats = await node.get_user_online_ip_list(email=email)
            if stats is None:
                return None

            return stats.ips
        except NodeAPIError as e:
            if e.code != 404:
                logger.error(f"Error getting IP list for user {email} on node {node_id}: {e}")
            return None

    async def sync_node_users(self, db: AsyncSession, node_id: int, flush_users: bool = False) -> NodeResponse:
        return await self._sync_node_users_impl(db, node_id, flush_users)

    async def clear_usage_data(self, db: AsyncSession, table: UsageTable, query: NodeClearUsageQuery):
        if query.start and query.end and query.start >= query.end:
            await self.raise_error(code=400, message="Start time must be before end time.")

        try:
            await clear_usage_data(db, table, query.start, query.end)
            return {"detail": f"All data from '{table}' has been deleted successfully."}
        except Exception as e:
            await self.raise_error(code=400, message=f"Deletion failed due to server error: {e!s}")

    async def _validate_node_then_release_db(self, db: AsyncSession, node_id: int) -> None:
        await self.get_validated_node(db, node_id, load_usage_logs=False)
        await db.close()

    async def update_node(self, db: AsyncSession, node_id: int) -> dict:
        await self._validate_node_then_release_db(db, node_id)
        return await self._update_node_api_impl(node_id)

    async def update_core(self, db: AsyncSession, node_id: int, node_core_update: NodeCoreUpdate) -> dict:
        await self._validate_node_then_release_db(db, node_id)
        return await self._update_core_impl(node_id, node_core_update)

    async def update_geofiles(self, db: AsyncSession, node_id: int, node_geofiles_update: NodeGeoFilesUpdate) -> dict:
        await self._validate_node_then_release_db(db, node_id)
        return await self._update_geofiles_impl(node_id, node_geofiles_update)

    async def _update_node_local(self, db_node: Node) -> None:
        await node_manager.update_node(db_node)

    async def _update_node_sync(self, db_node: Node) -> None:
        await self._update_node_local(db_node)
        await publish_node_sync("upsert", db_node.id)

    async def _update_node_remote(self, db_node: Node) -> None:
        await node_nats_client.publish("update_node", {"node_id": db_node.id})

    async def _remove_node_local(self, node_id: int) -> None:
        await node_manager.remove_node(node_id)
        forget_connect_pushes(node_id)
        await clear_bridge_memory_for_node(node_id)
        await clear_local_user_queue(node_id)

    async def _remove_node_sync(self, node_id: int) -> None:
        await self._remove_node_local(node_id)
        await publish_node_sync("remove", node_id)

    async def _remove_node_remote(self, node_id: int) -> None:
        await node_nats_client.publish("remove_node", {"node_id": node_id})

    async def _connect_nodes_bulk_local(
        self, db: AsyncSession, nodes: list[Node], *, force_start: bool = False, stop_first: bool = True
    ) -> None:
        if not nodes:
            return

        snapshot = current_connect_snapshot()
        reread = [
            await get_node_by_id(db, node.id, load_usage_logs=False, fresh_view=True)
            for node in nodes
            if node is not None
        ]
        nodes = [node for node in reread if node is not None]
        if not nodes:
            return
        core_ids = {core_id for node in nodes for core_id in self._node_core_ids(node)}
        cores_by_id, users_by_core = await self._get_core_users_map(db, core_ids, lazy=True)
        sem = asyncio.Semaphore(CONNECT_CONCURRENCY)

        async def connect_single(node: Node) -> dict | None:
            if node is None or node.status in (NodeStatus.disabled, NodeStatus.limited):
                return

            async with sem:
                try:
                    if not await NodeOperation._register_if_wanted(node):
                        return
                except NodeAPIError as e:
                    return {
                        "node_id": node.id,
                        "status": NodeStatus.error,
                        "message": e.detail,
                        "xray_version": "",
                        "node_version": "",
                        "old_status": node.status,
                    }
                except InvalidProxyURL:
                    return {
                        "node_id": node.id,
                        "status": NodeStatus.error,
                        "message": INVALID_PROXY_MESSAGE,
                        "xray_version": "",
                        "node_version": "",
                        "old_status": node.status,
                    }

                core_id = node.core_config_id or 1
                return await with_connect_snapshot(
                    snapshot,
                    self.connect_node(
                        node,
                        cores_by_id.get(core_id),
                        users_by_core.get(core_id, []),
                        self._extra_cores_for(node, cores_by_id, users_by_core),
                        force_start=force_start,
                        stop_first=stop_first,
                    ),
                )

        results = await asyncio.gather(*[connect_single(node) for node in nodes])

        # Filter out None results
        valid_results = [r for r in results if r is not None]

        nodes_dict = {node.id: node for node in nodes}

        notifications_to_send = []
        for result in valid_results:
            node = nodes_dict.get(result["node_id"])
            if not node:
                continue

            # Create lightweight notification object
            node_notif = NodeNotification(
                id=result["node_id"],
                name=node.name,
                xray_version=result.get("xray_version"),
                node_version=result.get("node_version"),
                message=result.get("message"),
            )

            notifications_to_send.append(
                {
                    "node": node_notif,
                    "status": result["status"],
                    "old_status": result["old_status"],
                }
            )

        # Bulk update all statuses in ONE query
        await bulk_update_node_status(db, valid_results)

        # Send notifications using pre-built objects
        for notif in notifications_to_send:
            if notif["status"] == NodeStatus.connected and notif["old_status"] != NodeStatus.connected:
                asyncio.create_task(notification.connect_node(notif["node"]))
                if notif["node"].message:
                    asyncio.create_task(notification.error_node(notif["node"]))
            elif notif["status"] == NodeStatus.error and notif["old_status"] != NodeStatus.error:
                asyncio.create_task(notification.error_node(notif["node"]))

    async def _connect_nodes_bulk_sync(
        self, db: AsyncSession, nodes: list[Node], *, force_start: bool = False, stop_first: bool = True
    ) -> None:
        await self._connect_nodes_bulk_local(db, nodes, force_start=force_start, stop_first=stop_first)
        for node in nodes:
            if node is not None and node.status not in (NodeStatus.disabled, NodeStatus.limited):
                await publish_node_sync("connect", node.id)

    async def _connect_nodes_bulk_remote(
        self, db: AsyncSession, nodes: list[Node], *, force_start: bool = False, stop_first: bool = True
    ) -> None:
        if not nodes:
            return
        await node_nats_client.publish(
            "connect_nodes_bulk",
            {"node_ids": [node.id for node in nodes], "force_start": force_start, "stop_first": stop_first},
        )

    async def recover_node(self, db: AsyncSession, node_id: int) -> None:
        """Repair a node, leaving status/notifications to consecutive health probes."""
        if (
            await self._connect_single_node_local(db, node_id, defer_health_status=True)
            and needs_shared_bridge_memory()
        ):
            await publish_node_sync("connect", node_id)

    async def _connect_single_node_local(
        self, db: AsyncSession, node_id: int, *, force_start: bool = False, defer_health_status: bool = False
    ) -> bool:
        snapshot = connect_snapshot()
        db_node = await get_node_by_id(db, node_id, load_usage_logs=False, fresh_view=True)
        if db_node is None or db_node.status in (NodeStatus.disabled, NodeStatus.limited):
            return False

        core_id = db_node.core_config_id or 1
        cores_by_id, users_by_core = await self._get_core_users_map(db, set(self._node_core_ids(db_node)), lazy=True)
        core = cores_by_id.get(core_id)
        users = users_by_core.get(core_id, [])
        extra_cores = self._extra_cores_for(db_node, cores_by_id, users_by_core)

        # Update node manager
        old_status = db_node.status
        try:
            if not await NodeOperation._register_if_wanted(db_node):
                return False
        except NodeAPIError as e:
            if defer_health_status:
                logger.warning("Node recovery failed for %s: %s", node_id, e.detail)
                return False
            # Update status to error using simple CRUD
            await update_node_status(
                db=db,
                db_node=db_node,
                status=NodeStatus.error,
                message=e.detail,
            )

            if old_status != NodeStatus.error:
                node_notif = NodeNotification(
                    id=db_node.id,
                    name=db_node.name,
                    message=e.detail,
                )
                asyncio.create_task(notification.error_node(node_notif))
            return False

        # Connect the node
        result = await with_connect_snapshot(
            snapshot, NodeOperation.connect_node(db_node, core, users, extra_cores, force_start=force_start)
        )

        if not result:
            return False
        if defer_health_status:
            # A successful Start/attach is not yet a sustained health recovery.
            # A failed repair must not bypass the health failure threshold either.
            return result["status"] == NodeStatus.connected

        # Update status using simple CRUD (NOT bulk!)
        await update_node_status(
            db=db,
            db_node=db_node,
            status=result["status"],
            message=result.get("message", ""),
            xray_version=result.get("xray_version", ""),
            node_version=result.get("node_version", ""),
        )

        # Send appropriate notification
        if result["status"] == NodeStatus.connected and result["old_status"] != NodeStatus.connected:
            node_notif = NodeNotification(
                id=db_node.id,
                name=db_node.name,
                xray_version=result.get("xray_version"),
                node_version=result.get("node_version"),
            )
            asyncio.create_task(notification.connect_node(node_notif))
            if result.get("message"):
                logger.warning(f'Node "{db_node.name}" connected but some additional cores failed: {result["message"]}')
                asyncio.create_task(
                    notification.error_node(
                        NodeNotification(id=db_node.id, name=db_node.name, message=result["message"])
                    )
                )
        elif result["status"] == NodeStatus.error and result["old_status"] != NodeStatus.error:
            node_notif = NodeNotification(
                id=db_node.id,
                name=db_node.name,
                message=result.get("message"),
            )
            asyncio.create_task(notification.error_node(node_notif))

        return True

    async def _connect_single_node_sync(self, db: AsyncSession, node_id: int, *, force_start: bool = False) -> None:
        if await self._connect_single_node_local(db, node_id, force_start=force_start):
            await publish_node_sync("connect", node_id)

    async def _connect_single_node_remote(self, db: AsyncSession, node_id: int, *, force_start: bool = False) -> None:
        await node_nats_client.publish("connect_node", {"node_id": node_id, "force_start": force_start})

    async def _disconnect_single_node_local(self, node_id: int) -> None:
        async with _connect_lock(node_id):
            await node_manager.remove_node(node_id)

    async def _disconnect_single_node_sync(self, node_id: int) -> None:
        await self._disconnect_single_node_local(node_id)
        await publish_node_sync("disconnect", node_id)

    async def _disconnect_single_node_remote(self, node_id: int) -> None:
        await node_nats_client.publish("disconnect_node", {"node_id": node_id})

    async def _restart_all_nodes_local(
        self, db: AsyncSession, admin: AdminDetails, core_id: int | None, *, stop_first: bool = True
    ) -> None:
        nodes, _ = await get_nodes(
            db,
            query=NodeListQuery(
                core_id=core_id,
                status=[NodeStatus.connected, NodeStatus.connecting, NodeStatus.error],
            ),
            load_usage_logs=False,
        )
        await self.connect_nodes_bulk(db, nodes, force_start=True, stop_first=stop_first)

    async def _restart_all_nodes_remote(
        self, db: AsyncSession, admin: AdminDetails, core_id: int | None, *, stop_first: bool = True
    ) -> None:
        await node_nats_client.publish(
            "connect_nodes_bulk", {"core_id": core_id, "force_start": True, "stop_first": stop_first}
        )

    async def _get_logs_local(self, node_id: int) -> Callable[[], AsyncIterator[asyncio.Queue]]:
        node = await node_manager.get_node(node_id)
        if node is None:
            await self.raise_error(message="Node not found", code=404)
        return node.stream_logs

    async def _get_logs_remote(self, node_id: int) -> Callable[[], AsyncIterator[asyncio.Queue]]:
        await self.raise_error(message="Node logs are only available via node-worker", code=409)

    async def _get_node_system_stats_local(self, node_id: int) -> NodeRealtimeStats:
        node = await node_manager.get_node(node_id)

        if node is None:
            await self.raise_error(message="Node not found", code=404)

        try:
            stats = await node.get_system_stats()
        except NodeAPIError as e:
            await self.raise_error(message=e.detail, code=node_error_status(e.code))

        if stats is None:
            await self.raise_error(message="Stats not found", code=404)

        return NodeRealtimeStats(
            mem_total=stats.mem_total,
            mem_used=stats.mem_used,
            cpu_cores=stats.cpu_cores,
            cpu_usage=stats.cpu_usage,
            incoming_bandwidth_speed=stats.incoming_bandwidth_speed,
            outgoing_bandwidth_speed=stats.outgoing_bandwidth_speed,
            uptime=stats.uptime,
        )

    async def _get_node_system_stats_remote(self, node_id: int) -> NodeRealtimeStats:
        try:
            data = await node_nats_client.request("get_node_system_stats", {"node_id": node_id})
            return NodeRealtimeStats.model_validate(data)
        except RuntimeError as exc:
            await self.handle_rpc_error(exc)

    async def _get_nodes_system_stats_local(self) -> dict[int, NodeRealtimeStats | None]:
        nodes = await node_manager.get_healthy_nodes()
        stats_tasks = {id: asyncio.create_task(self._get_node_stats_safe(id)) for id, _ in nodes}

        await asyncio.gather(*stats_tasks.values(), return_exceptions=True)

        results = {}
        for node_id, task in stats_tasks.items():
            if task.exception():
                results[node_id] = None
            else:
                results[node_id] = task.result()

        return results

    async def _get_nodes_system_stats_remote(self) -> dict[int, NodeRealtimeStats | None]:
        try:
            data = await node_nats_client.request("get_nodes_system_stats", {})
            return {
                int(node_id): (NodeRealtimeStats.model_validate(value) if value else None)
                for node_id, value in data.items()
            }
        except RuntimeError as exc:
            await self.handle_rpc_error(exc)

    async def _get_outbounds_latency_local(
        self, node_id: int, name: str = "", timeout: int | None = None
    ) -> NodeOutboundsLatencyResponse:
        node = await node_manager.get_node(node_id)

        if node is None:
            await self.raise_error(message="Node not found", code=404)

        try:
            latency = await node.get_outbounds_latency(name=name, timeout=timeout)
        except NodeAPIError as e:
            await self.raise_error(message=e.detail, code=node_error_status(e.code))

        if latency is None:
            await self.raise_error(message="Latency not found", code=404)

        return NodeOutboundsLatencyResponse(
            latencies=[
                {
                    "name": item.name,
                    "alive": item.alive,
                    "delay": item.delay,
                    "link": item.link,
                    "last_seen_time": item.last_seen_time,
                    "last_try_time": item.last_try_time,
                    "source": item.source,
                }
                for item in latency.latencies
            ]
        )

    async def _get_outbounds_latency_remote(
        self, node_id: int, name: str = "", timeout: int | None = None
    ) -> NodeOutboundsLatencyResponse:
        try:
            data = await node_nats_client.request(
                "get_outbounds_latency",
                {"node_id": node_id, "name": name, "timeout": timeout},
                timeout=timeout,
            )
            return NodeOutboundsLatencyResponse.model_validate(data)
        except RuntimeError as exc:
            await self.handle_rpc_error(exc)

    async def _get_user_online_stats_local(self, db: AsyncSession, node_id: int, user_id: int) -> dict[int, int]:
        db_user = await get_user_by_id(db, user_id)
        if db_user is None:
            await self.raise_error(message="User not found", code=404)

        node = await node_manager.get_node(node_id)

        if node is None:
            await self.raise_error(message="Node not found", code=404)

        try:
            stats = await node.get_user_online_stats(email=f"{db_user.id}")
        except NodeAPIError as e:
            await self.raise_error(message=e.detail, code=node_error_status(e.code))

        if stats is None:
            await self.raise_error(message="Stats not found", code=404)

        return {node_id: stats.value}

    async def _get_user_online_stats_remote(self, db: AsyncSession, node_id: int, user_id: int) -> dict[int, int]:
        try:
            return await node_nats_client.request("get_user_online_stats", {"node_id": node_id, "user_id": user_id})
        except RuntimeError as exc:
            await self.handle_rpc_error(exc)

    async def _get_user_ip_list_local(self, db: AsyncSession, node_id: int, user_id: int) -> UserIPList:
        db_user = await get_user_by_id(db, user_id)
        if db_user is None:
            await self.raise_error(message="User not found", code=404)

        email = f"{db_user.id}"
        ips = await self._get_node_user_ip_list_safe(node_id, email)

        if ips is None:
            await self.raise_error(message="Node unavailable or user not found", code=404)

        return UserIPList(ips=ips)

    async def _get_user_ip_list_remote(self, db: AsyncSession, node_id: int, user_id: int) -> UserIPList:
        try:
            data = await node_nats_client.request("get_user_ip_list", {"node_id": node_id, "user_id": user_id})
            return UserIPList.model_validate(data)
        except RuntimeError as exc:
            await self.handle_rpc_error(exc)

    async def _get_user_ip_list_all_local(self, db: AsyncSession, user_id: int) -> UserIPListAll:
        db_user = await get_user_by_id(db, user_id)
        if db_user is None:
            await self.raise_error(message="User not found", code=404)

        nodes = await node_manager.get_healthy_nodes()
        email = f"{db_user.id}"

        ip_list_tasks = {id: asyncio.create_task(self._get_node_user_ip_list_safe(id, email)) for id, _ in nodes}

        await asyncio.gather(*ip_list_tasks.values(), return_exceptions=True)

        results = {}
        for node_id, task in ip_list_tasks.items():
            if task.exception() or task.result() is None:
                continue
            else:
                results[node_id] = UserIPList(ips=task.result())

        return UserIPListAll(nodes=results)

    async def _get_user_ip_list_all_remote(self, db: AsyncSession, user_id: int) -> UserIPListAll:
        try:
            data = await node_nats_client.request("get_user_ip_list_all", {"user_id": user_id})
            return UserIPListAll.model_validate(data)
        except RuntimeError as exc:
            await self.handle_rpc_error(exc)

    async def _sync_node_users_local(self, db: AsyncSession, node_id: int, flush_users: bool) -> NodeResponse:
        db_node = await self.get_validated_node(db, node_id=node_id)

        if db_node.status != NodeStatus.connected:
            await self.raise_error(message="Node is not connected", code=406)

        pg_node = await node_manager.get_node(node_id)
        if pg_node is None:
            await self.raise_error(message="Node is not connected", code=409)

        try:
            cores_by_id, users_by_core = await self._get_core_users_map(
                db, set(self._node_core_ids(db_node)), lazy=True
            )
            users = node_users_loader(
                users_by_core.get(db_node.core_config_id or 1, []),
                self._extra_cores_for(db_node, cores_by_id, users_by_core),
            )
            if await node_manager.sync_full(node_id, users, flush_pending=flush_users) is None:
                await self.raise_error(message="Node is not connected", code=409)
        except UserSyncPreemptedError:
            # An edit of the node's connection or its removal won; this is not a node fault,
            # so the node status is left alone and the admin can simply retry.
            await self.raise_error(
                message="User sync was interrupted by a change to the node's connection or its removal, retry",
                code=409,
            )
        except FullSyncConflictError as e:
            # Another snapshot (a Start, a repair) holds the node's fence (409), or deliveries are
            # still in flight past their lease (503); nothing reached the node.
            await self.raise_error(message=e.detail, code=e.code)
        except NodeAPIError as e:
            await update_node_status(db=db, db_node=db_node, status=NodeStatus.error, message=e.detail)
            await self.raise_error(message=e.detail, code=node_error_status(e.code))

        return NodeResponse.model_validate(db_node)

    async def _sync_node_users_remote(self, db: AsyncSession, node_id: int, flush_users: bool) -> NodeResponse:
        await node_nats_client.publish(
            "sync_node_users",
            {"node_id": node_id, "flush_users": flush_users},
        )
        return NodeResponse.model_validate(await self.get_validated_node(db, node_id))

    @staticmethod
    async def refresh_node_versions(
        db: AsyncSession,
        node_id: int,
        node: PasarGuardNode,
        timeout: int | None = None,
        *,
        persist: bool = True,
    ) -> bool:
        """Read live versions and persist them only for the current connected node."""
        info = await node.info(timeout=timeout)
        if info is None or not info.started or not info.node_version or not info.core_version:
            return False
        if await node_manager.get_node(node_id) is not node:
            return False

        if not persist:
            await node.set_reported_versions(info.node_version, info.core_version)
            return True

        db_node = await get_node_by_id(db, node_id, load_usage_logs=False)
        if db_node is None or db_node.status != NodeStatus.connected:
            return False
        if db_node.node_version != info.node_version or db_node.xray_version != info.core_version:
            db_node.node_version = info.node_version
            db_node.xray_version = info.core_version
            await db.commit()
        await node.set_reported_versions(info.node_version, info.core_version)
        return True

    @staticmethod
    async def _mark_node_reconnecting_after_update(db: AsyncSession, node_id: int, node: PasarGuardNode) -> None:
        """Leave a restarted node for the health job to attach or start."""
        db_node = await get_node_by_id(db, node_id, load_usage_logs=False)
        if db_node is None or db_node.status in (NodeStatus.disabled, NodeStatus.limited):
            return
        await update_node_status(
            db,
            db_node,
            NodeStatus.connecting,
            message="",
            xray_version="",
            node_version="",
        )
        await node.set_health(Health.NOT_CONNECTED)

    async def _update_node_api_local(self, node_id: int) -> dict:
        node = await node_manager.get_node(node_id)
        if node is None:
            await self.raise_error(message="Node not found", code=404)
        try:
            response = await node.update_node()
        except NodeAPIError as e:
            await self.raise_error(message=e.detail, code=_maintenance_error_status(e))
        # The request's session was released before the RPC, so write in a fresh one.
        async with GetDB() as fresh:
            await self._mark_node_reconnecting_after_update(fresh, node_id, node)
        return response.json()

    async def _maintenance_request(self, action: str, payload: dict) -> dict:
        try:
            return await node_nats_client.request(action, payload, timeout=_maintenance_rpc_timeout())
        except nats.errors.TimeoutError:
            await self.raise_error(message=f"Node {action} timed out", code=504)
        except nats.errors.Error as exc:
            await self.raise_error(message=f"Node {action} unavailable: {exc}", code=503)
        except RuntimeError as exc:
            if not hasattr(exc, "code"):
                await self.raise_error(message=str(exc), code=503)
            await self.handle_rpc_error(exc)

    async def _update_node_api_remote(self, node_id: int) -> dict:
        return await self._maintenance_request("update_node_api", {"node_id": node_id})

    async def _update_core_local(self, node_id: int, node_core_update: NodeCoreUpdate) -> dict:
        node = await node_manager.get_node(node_id)
        if node is None:
            await self.raise_error(message="Node not found", code=404)
        try:
            response = await node.update_core(node_core_update.model_dump(mode="json"))
        except NodeAPIError as e:
            await self.raise_error(message=e.detail, code=_maintenance_error_status(e))
        async with GetDB() as fresh:
            await self._mark_node_reconnecting_after_update(fresh, node_id, node)
        return response.json()

    async def _update_core_remote(self, node_id: int, node_core_update: NodeCoreUpdate) -> dict:
        return await self._maintenance_request(
            "update_core", {"node_id": node_id, "core_update": node_core_update.model_dump(mode="json")}
        )

    async def _update_geofiles_local(self, node_id: int, node_geofiles_update: NodeGeoFilesUpdate) -> dict:
        node = await node_manager.get_node(node_id)
        if node is None:
            await self.raise_error(message="Node not found", code=404)
        try:
            response = await node.update_geofiles(node_geofiles_update.model_dump(mode="json"))
        except NodeAPIError as e:
            await self.raise_error(message=e.detail, code=_maintenance_error_status(e))
        async with GetDB() as fresh:
            await self._mark_node_reconnecting_after_update(fresh, node_id, node)
        return response.json()

    async def _update_geofiles_remote(self, node_id: int, node_geofiles_update: NodeGeoFilesUpdate) -> dict:
        return await self._maintenance_request(
            "update_geofiles", {"node_id": node_id, "geofiles_update": node_geofiles_update.model_dump(mode="json")}
        )

    async def bulk_remove_nodes(
        self, db: AsyncSession, bulk_nodes: BulkNodeSelection, admin: AdminDetails
    ) -> RemoveNodesResponse:
        """Remove multiple nodes by ID"""
        db_nodes = []
        for node_id in bulk_nodes.ids:
            db_node = await self.get_validated_node(db, node_id)
            db_nodes.append(db_node)

        node_responses = [NodeResponse.model_validate(n) for n in db_nodes]

        results = await retire_nodes(db, db_nodes, here=self._retires_here)
        outcomes = {result.node_id: result.outcome for result in results if not result.refused}

        # Notify
        for node_response in node_responses:
            if outcomes.get(node_response.id, Retirement.absent) is Retirement.absent:
                continue
            logger.info(f'Node "{node_response.name}" with id "{node_response.id}" deleted by admin "{admin.username}"')
            asyncio.create_task(notification.remove_node(node_response, admin.username))

        refusal = refusal_message(results)
        if refusal:
            await self.raise_error(refusal, 503)
        deleted = [node_response.name for node_response in node_responses if node_response.id in outcomes]
        return RemoveNodesResponse(nodes=deleted, count=len(deleted))

    async def _get_validated_nodes(self, db: AsyncSession, node_ids: list[int] | set[int]) -> list[Node]:
        if not node_ids:
            return []

        ids_list = list(node_ids)
        db_nodes, _ = await get_nodes(db, NodeListQuery(ids=ids_list, limit=len(ids_list)), load_usage_logs=False)

        found_ids = {n.id for n in db_nodes}
        missing = set(ids_list) - found_ids
        if missing:
            await self.raise_error(message="Node not found", code=404)

        return list(db_nodes)

    @staticmethod
    def _build_bulk_action_response(nodes: list[Node | NodeResponse]) -> BulkNodesActionResponse:
        names = [node.name for node in nodes]
        return BulkNodesActionResponse(nodes=names, count=len(names))

    @staticmethod
    def _build_node_modify_payload(node: Node) -> NodeModify:
        return NodeModify(
            name=node.name,
            address=node.address,
            port=node.port,
            api_port=node.api_port,
            usage_coefficient=node.usage_coefficient,
            connection_type=node.connection_type,
            server_ca=node.server_ca,
            keep_alive=node.keep_alive,
            core_config_id=node.core_config_id,
            additional_core_config_ids=node.additional_core_config_ids,
            api_key=node.api_key,
            data_limit=node.data_limit,
            data_limit_reset_strategy=node.data_limit_reset_strategy,
            reset_time=node.reset_time,
            default_timeout=node.default_timeout,
            internal_timeout=node.internal_timeout,
        )

    async def bulk_set_nodes_status(
        self,
        db: AsyncSession,
        bulk_nodes: BulkNodeSelection,
        admin: AdminDetails,
        *,
        status: NodeStatus,
    ) -> BulkNodesActionResponse:
        db_nodes = await self._get_validated_nodes(db, bulk_nodes.ids)
        nodes_to_update = [db_node for db_node in db_nodes if db_node.status != status]

        for db_node in nodes_to_update:
            payload = self._build_node_modify_payload(db_node)
            payload.status = status
            await self.modify_node(db, node_id=db_node.id, modified_node=payload, admin=admin)

        action = "enabled" if status != NodeStatus.disabled else "disabled"
        for db_node in nodes_to_update:
            logger.info(f'Node "{db_node.name}" bulk {action} by admin "{admin.username}"')

        return self._build_bulk_action_response(nodes_to_update)

    async def bulk_reset_nodes_usage(
        self, db: AsyncSession, bulk_nodes: BulkNodeSelection, admin: AdminDetails
    ) -> BulkNodesActionResponse:
        db_nodes = await self._get_validated_nodes(db, bulk_nodes.ids)
        old_usages = {node.id: (node.uplink, node.downlink) for node in db_nodes}
        limited_node_ids = {node.id for node in db_nodes if node.status == NodeStatus.limited}

        db_nodes = await bulk_reset_node_usage(db, db_nodes)

        for db_node in db_nodes:
            if db_node.id in limited_node_ids:
                await self.connect_single_node(db, db_node.id)
                db_node = await self.get_validated_node(db=db, node_id=db_node.id)

            node = NodeResponse.model_validate(db_node)
            old_uplink, old_downlink = old_usages[db_node.id]
            asyncio.create_task(notification.reset_node_usage(node, admin.username, old_uplink, old_downlink))
            logger.info(f'Node "{db_node.name}" usage reset by admin "{admin.username}"')

        return self._build_bulk_action_response(db_nodes)

    async def bulk_restart_nodes(
        self, db: AsyncSession, bulk_nodes: BulkNodeSelection, admin: AdminDetails
    ) -> BulkNodesActionResponse:
        db_nodes = await self._get_validated_nodes(db, bulk_nodes.ids)

        await self.connect_nodes_bulk(db, db_nodes, force_start=True)

        for db_node in db_nodes:
            logger.info(f'Node "{db_node.name}" restarted by admin "{admin.username}"')

        return self._build_bulk_action_response(db_nodes)

    async def bulk_update_nodes(
        self, db: AsyncSession, bulk_nodes: BulkNodeSelection, admin: AdminDetails
    ) -> BulkNodesActionResponse:
        db_nodes = await self._get_validated_nodes(db, bulk_nodes.ids)
        targets = [(db_node.id, db_node.name) for db_node in db_nodes]

        updated_names = []
        errors = []
        for node_id, node_name in targets:
            try:
                await self.update_node(db, node_id)
            except HTTPException as exc:
                errors.append(exc)
                logger.warning(f'Node "{node_name}" bulk update failed by admin "{admin.username}": {exc}')
                continue

            updated_names.append(node_name)
            logger.info(f'Node "{node_name}" updated by admin "{admin.username}"')

        if not updated_names and errors:
            raise errors[0]

        return BulkNodesActionResponse(nodes=updated_names, count=len(updated_names))
