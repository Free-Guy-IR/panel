from app.models.core import CoreType
from app.utils.l2tp import ensure_l2tp_core_material


def apply_l2tp_core_config(core) -> None:
    if core.type == CoreType.l2tp:
        core.config = ensure_l2tp_core_material(core.config)
