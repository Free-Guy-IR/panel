from app.utils.logger import get_logger

logger = get_logger("node-user")

REQUIRED_CREDENTIALS = {
    "vmess": ("id",),
    "vless": ("id",),
    "trojan": ("password",),
    "shadowsocks": ("password",),
    "wireguard": ("public_key",),
    "hysteria": ("auth",),
    "hysteria2": ("password",),
    "open_vpn": ("password",),
    "tuic": ("uuid", "password"),
    "l2tp": ("password",),
    "masque": ("pass",),
}


def omit_empty_credentials(proxy):
    for name, required in REQUIRED_CREDENTIALS.items():
        section = getattr(proxy, name)
        if any(not getattr(section, attribute) for attribute in required):
            proxy.ClearField(name)
    return proxy


def usable_credentials(proxy_kwargs: dict, user_id) -> dict:
    masque_pass = proxy_kwargs.get("masque_pass")
    if masque_pass is None or (isinstance(masque_pass, str) and masque_pass):
        return proxy_kwargs
    logger.warning(f"Dropping an unusable stored MASQUE password for user {user_id}")
    return {key: value for key, value in proxy_kwargs.items() if key != "masque_pass"}
