import asyncio

from packaging.version import InvalidVersion, Version
from PasarGuardNodeBridge import NodeAPIError, PasarGuardNode
from PasarGuardNodeBridge.common import service_pb2 as service

from app.db import AsyncSession
from app.db.crud.node import get_inbounds_usage as fetch_inbounds_usage, get_nodes
from app.db.models import Node, NodeStatus
from app.models.admin import AdminDetails
from app.models.core import CoreType
from app.models.node import InboundUsageQuery, NodeListQuery
from app.models.stats import InboundUsageStatsList
from app.node.user import core_users
from app.utils.logger import get_logger

logger = get_logger("node-operation")

_CONNECT_LOCKS: dict[int, asyncio.Lock] = {}


def _connect_lock(node_id: int) -> asyncio.Lock:
    return _CONNECT_LOCKS.setdefault(node_id, asyncio.Lock())


L2TP_MIN_NODE_VERSION = "0.6.4"


def _node_lacks_l2tp(node_version: str) -> bool:
    if not node_version:
        return True
    try:
        return Version(node_version) < Version(L2TP_MIN_NODE_VERSION)
    except InvalidVersion:
        return True


async def _known_node_version(pg_node: PasarGuardNode) -> str:
    known = await pg_node.node_version()
    if known:
        return known
    try:
        info = await pg_node.info()
    except Exception:
        return ""
    if info is None:
        return ""
    return info.node_version or ""


def _l2tp_unsupported_message(node_version: str) -> str:
    if node_version:
        reason = f"this node runs image v{node_version}, which has no L2TP backend"
    else:
        reason = "this node did not report an image version, so L2TP support cannot be confirmed"
    return f"{reason}. Rebuild and redeploy the node image (v{L2TP_MIN_NODE_VERSION} or newer), then reconnect."


_MULTI_INSTANCE_BACKENDS = {
    CoreType.wg: service.BackendType.WIREGUARD,
    CoreType.singbox: service.BackendType.SING_BOX,
    CoreType.openvpn: service.BackendType.OPEN_VPN,
    CoreType.mtproto: service.BackendType.MTPROTO,
    CoreType.l2tp: service.BackendType.L2TP,
}

_BACKEND_TYPE_BY_CORE = {CoreType.xray: service.BackendType.XRAY, **_MULTI_INSTANCE_BACKENDS}


class NodeExtraCoresMixin:
    @staticmethod
    def _node_core_ids(db_node: Node) -> list[int]:
        core_ids = [getattr(db_node, "core_config_id", None) or 1]
        for core_id in getattr(db_node, "additional_core_config_ids", None) or []:
            if core_id not in core_ids:
                core_ids.append(core_id)
        return core_ids

    async def _validate_additional_cores(
        self, db: AsyncSession, core_ids: list[int] | None, primary_id: int | None
    ) -> None:
        if not core_ids:
            return

        allowed = ", ".join(sorted(core_type.value for core_type in _MULTI_INSTANCE_BACKENDS))
        primary_core = await self.get_validated_core_config(db, primary_id or 1)
        taken_types = {primary_core.type: primary_core.name}
        seen = set()

        for core_id in core_ids:
            if core_id == (primary_id or 1):
                await self.raise_error(message="A core cannot be both the primary and an additional core", code=400)
            if core_id in seen:
                await self.raise_error(message=f"Core {core_id} is listed twice as an additional core", code=400)
            seen.add(core_id)

            db_core = await self.get_validated_core_config(db, core_id)
            if db_core.type not in _MULTI_INSTANCE_BACKENDS:
                await self.raise_error(
                    message=f'Core "{db_core.name}" is a "{db_core.type.value}" core; only {allowed} cores can run alongside another core',
                    code=400,
                )
            if db_core.type in taken_types:
                await self.raise_error(
                    message=f'A node can run only one "{db_core.type.value}" core, and "{taken_types[db_core.type]}" already uses it',
                    code=400,
                )
            taken_types[db_core.type] = db_core.name

    @classmethod
    async def _node_union_users(cls, db: AsyncSession, db_node: Node) -> list:
        core_ids = cls._node_core_ids(db_node)
        from app.core.manager import core_manager

        resolved_cores = await core_manager.get_cores(set(core_ids) | {1})
        default_core = resolved_cores.get(1)

        inbound_tags, protocols = [], set()
        for core_id in core_ids:
            core = resolved_cores.get(core_id) or default_core
            if core is None:
                continue
            inbound_tags.extend(core.inbounds)
            protocols.update(core.protocols)

        return await core_users(db=db, inbound_tags=inbound_tags, allowed_protocols=frozenset(protocols))

    @classmethod
    def _extra_cores_for(cls, db_node: Node, cores_by_id: dict, users_by_core: dict) -> list[tuple]:
        primary_id = getattr(db_node, "core_config_id", None) or 1
        return [
            (core_id, cores_by_id.get(core_id), users_by_core.get(core_id, []))
            for core_id in cls._node_core_ids(db_node)
            if core_id != primary_id
        ]

    @staticmethod
    async def _running_backend_types(pg_node: PasarGuardNode) -> set:
        try:
            listed = await pg_node.list_backends()
        except Exception:
            return set()
        if listed is None:
            return set()

        known = set(_BACKEND_TYPE_BY_CORE.values())
        running, unknown = set(), []
        for backend_type in listed.types:
            if backend_type in known:
                running.add(backend_type)
            else:
                unknown.append(backend_type)
        if unknown:
            logger.info(f"Node reports backend type(s) this panel does not manage, leaving them alone: {unknown}")
        return running

    @classmethod
    async def _reconcile_extra_cores(cls, db: AsyncSession, pg_node: PasarGuardNode, db_node: Node) -> str:
        primary_id = getattr(db_node, "core_config_id", None) or 1
        extra_ids = [core_id for core_id in cls._node_core_ids(db_node) if core_id != primary_id]

        cores_by_id, users_by_core = await cls._get_core_users_map(db, set(extra_ids) | {primary_id})
        primary_core = cores_by_id.get(primary_id)

        extra_cores = [(core_id, cores_by_id.get(core_id), users_by_core.get(core_id, [])) for core_id in extra_ids]

        problems = [
            await cls._remove_surplus_backends(
                pg_node, db_node, extra_cores, primary_core.type if primary_core is not None else None
            ),
            await cls._add_extra_cores(pg_node, db_node, extra_cores),
        ]
        return "; ".join(p for p in problems if p)[:1024]

    @staticmethod
    def _desired_extra_backends(extra_cores: list[tuple] | None) -> dict:
        desired = {}
        for core_id, extra_core, extra_users in extra_cores or []:
            if extra_core is None:
                continue
            backend_type = _MULTI_INSTANCE_BACKENDS.get(extra_core.type)
            if backend_type is not None:
                desired[backend_type] = (core_id, extra_core, extra_users)
        return desired

    @classmethod
    async def _remove_surplus_backends(
        cls, pg_node: PasarGuardNode, db_node: Node, extra_cores: list[tuple] | None, primary_core_type
    ) -> str:
        primary_type = _BACKEND_TYPE_BY_CORE.get(primary_core_type)
        if primary_type is None:
            logger.warning(
                f'Not reconciling backends on "{db_node.name}": its primary core type is unknown, '
                f"so a surplus backend cannot be told apart from the primary one"
            )
            return ""

        running = await cls._running_backend_types(pg_node)
        if not running:
            return ""

        desired = set(cls._desired_extra_backends(extra_cores))
        problems = []

        for backend_type in sorted(running, key=lambda item: int(item)):
            if backend_type in desired or backend_type == primary_type:
                continue
            try:
                await pg_node.remove_backend(backend_type=backend_type)
                logger.info(f'Removed backend "{backend_type}" from "{db_node.name}" node; it is no longer assigned')
            except NodeAPIError as e:
                problems.append(f"removing {backend_type}: {e.detail}")
            except Exception as e:
                problems.append(f"removing {backend_type}: {e}")

        return "; ".join(problems)

    @classmethod
    async def _add_extra_cores(
        cls, pg_node: PasarGuardNode, db_node: Node, extra_cores: list[tuple] | None, *, restart_running: bool = False
    ) -> str:
        if not extra_cores:
            return ""

        problems = []
        already_running = await cls._running_backend_types(pg_node)

        for core_id, extra_core, extra_users in extra_cores or []:
            if extra_core is None:
                logger.warning(f'Extra core "{core_id}" for node "{db_node.name}" could not be resolved')
                problems.append(f"core {core_id} not found")
                continue

            backend_type = _MULTI_INSTANCE_BACKENDS.get(extra_core.type)
            if backend_type is None:
                problems.append(f"core {core_id} skipped ({extra_core.type.value} cannot run alongside another core)")
                continue

            if backend_type in already_running:
                if not restart_running:
                    logger.debug(f'Core "{core_id}" is already running on "{db_node.name}" node')
                    continue
                try:
                    await pg_node.remove_backend(backend_type=backend_type)
                    already_running.discard(backend_type)
                except NodeAPIError as e:
                    problems.append(f"core {core_id}: restarting backend: {e.detail}")
                    continue
                except Exception as e:
                    problems.append(f"core {core_id}: restarting backend: {e}")
                    continue

            if extra_core.type == CoreType.l2tp:
                known_version = await _known_node_version(pg_node)
                if _node_lacks_l2tp(known_version):
                    problems.append(f"core {core_id}: {_l2tp_unsupported_message(known_version)}")
                    continue

            try:
                await pg_node.add_backend(
                    config=extra_core.to_str(),
                    backend_type=backend_type,
                    users=extra_users,
                )
                already_running.add(backend_type)
                logger.info(f'Added {extra_core.type.value} core "{core_id}" to "{db_node.name}" node')
            except NodeAPIError as e:
                logger.error(f'Failed to add core "{core_id}" to "{db_node.name}": {e.detail}')
                problems.append(f"core {core_id}: {e.detail}")
            except Exception as e:
                logger.error(f'Failed to add core "{core_id}" to "{db_node.name}": {e}')
                problems.append(f"core {core_id}: {e}")

        return "; ".join(problems)[:1024]

    async def restart_nodes_by_ids(self, db: AsyncSession, node_ids: list[int], admin: AdminDetails) -> None:
        if not node_ids:
            return
        nodes, _ = await get_nodes(
            db,
            query=NodeListQuery(
                ids=node_ids,
                status=[NodeStatus.connected, NodeStatus.connecting, NodeStatus.error],
            ),
            load_usage_logs=False,
        )
        await self.connect_nodes_bulk(db, nodes)
        logger.info(f'Nodes {node_ids} restarted by admin "{admin.username}"')

    async def get_inbounds_usage(
        self,
        db: AsyncSession,
        query: InboundUsageQuery,
    ) -> InboundUsageStatsList:
        start, end = await self.validate_dates(query.start, query.end, True)
        return await fetch_inbounds_usage(
            db,
            start,
            end,
            period=query.period,
            inbound_tag=query.inbound_tag,
            node_id=query.node_id,
        )
