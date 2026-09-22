from app.db import AsyncSession
from app.fork.cores.singbox_guard import unsafe_singbox_core_message, unsafe_singbox_core_nodes_message
from app.fork.crud.core import get_node_versions_by_core
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
