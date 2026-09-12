from importlib import import_module

from app.fork.registry import extra_subscription_formats, register_subscription_format


def _first(attr: str, *modules: str):
    last: Exception | None = None
    for name in modules:
        try:
            return getattr(import_module(name), attr)
        except (ModuleNotFoundError, ImportError) as exc:
            last = exc
    if last is not None:
        raise last
    raise ModuleNotFoundError(attr)


if "openvpn" not in extra_subscription_formats():
    OpenVPNConfiguration = _first(
        "OpenVPNConfiguration",
        "app.fork.subscription.openvpn",
        "app.subscription.openvpn",
    )
    register_subscription_format("openvpn", lambda _templates=None: OpenVPNConfiguration())

if "l2tp" not in extra_subscription_formats():
    L2TPConfiguration = _first(
        "L2TPConfiguration",
        "app.fork.subscription.l2tp",
        "app.subscription.l2tp",
    )
    register_subscription_format("l2tp", lambda _templates=None: L2TPConfiguration())
