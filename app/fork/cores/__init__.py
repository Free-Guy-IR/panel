from app.db.models import CoreType
from app.fork.cores.l2tp import L2TPConfig
from app.fork.cores.mtproto import MTProtoConfig
from app.fork.cores.openvpn import OpenVPNConfig
from app.fork.cores.singbox import SingBoxConfig
from app.fork.registry import extra_core_types, register_core_type

register_core_type(CoreType.singbox, SingBoxConfig)
register_core_type(CoreType.openvpn, OpenVPNConfig)
register_core_type(CoreType.mtproto, MTProtoConfig)
register_core_type(CoreType.l2tp, L2TPConfig)


def fork_core_classes() -> dict:
    return extra_core_types()


def register_fork_cores(core_classes: dict) -> None:
    core_classes.update(extra_core_types())
