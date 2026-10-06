REQUIRED_CREDENTIALS = {
    "vmess": ("id",),
    "vless": ("id",),
    "trojan": ("password",),
    "shadowsocks": ("password",),
    "wireguard": ("public_key",),
    "hysteria": ("auth",),
    "hysteria2": ("password",),
    "open_vpn": ("password",),
    "mtproto": ("secret",),
    "tuic": ("uuid", "password"),
    "l2tp": ("password",),
}


def omit_empty_credentials(proxy):
    for name, required in REQUIRED_CREDENTIALS.items():
        section = getattr(proxy, name)
        if any(not getattr(section, attribute) for attribute in required):
            proxy.ClearField(name)
    return proxy
