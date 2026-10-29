from collections.abc import Iterable

from sqlalchemy import select, update

from app.db import AsyncSession
from app.db.models import CoreConfig, Node, node_additional_cores_association
from app.fork.cores.singbox_guard import unsafe_singbox_core_message, unsafe_singbox_core_nodes_message
from app.fork.crud.core import get_node_versions_by_core
from app.fork.proxy_secrets.locks import read_committed_rows
from app.models.core import CoreType
from app.utils.l2tp import ensure_l2tp_core_material


def apply_l2tp_core_config(core) -> None:
    if core.type == CoreType.l2tp:
        core.config = ensure_l2tp_core_material(core.config)


async def guard_singbox_user_accounting(db: AsyncSession, core, core_id: int | None = None) -> None:
    core_name = getattr(core, "name", None)
    message = unsafe_singbox_core_message(core.type, core_name, core.config)
    if message:
        raise ValueError(message)
    if core_id is None:
        return
    message = unsafe_singbox_core_nodes_message(
        core.type, core_name, core.config, await get_node_versions_by_core(db, core_id)
    )
    if message:
        raise ValueError(message)


async def _hold_core_rows(db: AsyncSession, core_ids: list[int]) -> None:
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(update(CoreConfig).where(CoreConfig.id.in_(core_ids)).values(name=CoreConfig.name))
        return
    await db.execute(select(CoreConfig.id).where(CoreConfig.id.in_(core_ids)).with_for_update())


async def _nodes_using_cores(db: AsyncSession, core_ids: list[int]) -> list[tuple[int, int, str, str]]:
    association = node_additional_cores_association
    primary = select(Node.core_config_id, Node.id, Node.name).where(Node.core_config_id.in_(core_ids))
    additional = (
        select(association.c.core_config_id, Node.id, Node.name)
        .join(Node, Node.id == association.c.node_id)
        .where(association.c.core_config_id.in_(core_ids))
    )
    uses = [
        (core_id, node_id, name, "primary core") for core_id, node_id, name in await read_committed_rows(db, primary)
    ]
    uses += [
        (core_id, node_id, name, "additional core")
        for core_id, node_id, name in await read_committed_rows(db, additional)
    ]
    return sorted(uses, key=lambda use: (use[0], use[1], use[3]))


async def cores_in_use_message(db: AsyncSession, cores: Iterable[CoreConfig]) -> str | None:
    names = {core.id: core.name for core in cores}
    if not names:
        return None
    core_ids = sorted(names)
    await _hold_core_rows(db, core_ids)
    uses = await _nodes_using_cores(db, core_ids)
    if not uses:
        return None
    blocked = []
    for core_id in core_ids:
        nodes = [f'"{name}" (id {node_id}, {role})' for used_id, node_id, name, role in uses if used_id == core_id]
        if nodes:
            blocked.append(f'core "{names[core_id]}" (id {core_id}) is used by node {", ".join(nodes)}')
    return (
        f"Not deleted: {'; '.join(blocked)}. Move these nodes to another core first; "
        "a deleted core would leave them on the Default Core Config."
    )
