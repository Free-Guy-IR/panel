from app.fork.registry import register_subscription_format
from app.fork.subscription.l2tp import L2TPConfiguration
from app.fork.subscription.openvpn import OpenVPNConfiguration
from app.fork.subscription.outline import OutlineConfiguration

register_subscription_format("openvpn", lambda _templates=None: OpenVPNConfiguration())
register_subscription_format("l2tp", lambda _templates=None: L2TPConfiguration())


def fork_subscription_formats() -> dict:
    return {
        "openvpn": OpenVPNConfiguration,
        "l2tp": L2TPConfiguration,
        "outline": OutlineConfiguration,
    }


def register_fork_subscription_formats(formats: dict) -> None:
    formats.update(fork_subscription_formats())
