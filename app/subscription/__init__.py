from app.fork.registry import register_subscription_format
from app.fork.subscription import L2TPConfiguration, OpenVPNConfiguration, OutlineConfiguration

from .base import BaseSubscription
from .clash import ClashConfiguration, ClashMetaConfiguration
from .links import StandardLinks
from .singbox import SingBoxConfiguration
from .wireguard import WireGuardConfiguration
from .xray import XrayConfiguration

register_subscription_format("openvpn", lambda _templates=None: OpenVPNConfiguration())
register_subscription_format("l2tp", lambda _templates=None: L2TPConfiguration())

__all__ = [
    "BaseSubscription",
    "ClashConfiguration",
    "ClashMetaConfiguration",
    "L2TPConfiguration",
    "OpenVPNConfiguration",
    "OutlineConfiguration",
    "SingBoxConfiguration",
    "StandardLinks",
    "WireGuardConfiguration",
    "XrayConfiguration",
]
