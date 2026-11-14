import io
import zipfile

from app.fork.config_text import all_safe_config_values, is_safe_config_value
from app.models.subscription import SubscriptionInboundData
from app.subscription.base import BaseSubscription
from app.utils.logger import get_logger

logger = get_logger("openvpn-subscription")

LINE_FIELDS = ("address", "port", "protocol", "cipher", "auth", "username", "password")
BLOCK_FIELDS = ("ca_cert", "tls_crypt_key")


def _safe_component(component: dict) -> dict | None:
    if not all_safe_config_values(component.get(field) for field in LINE_FIELDS):
        return None
    if not all(all_safe_config_values(str(component.get(field)).strip().splitlines()) for field in BLOCK_FIELDS):
        return None
    dns_servers = component.get("dns_servers") or []
    safe_dns = [dns for dns in dns_servers if is_safe_config_value(dns)]
    if len(safe_dns) != len(dns_servers):
        logger.warning(f"Dropping {len(dns_servers) - len(safe_dns)} OpenVPN DNS value(s) that would break the profile")
    return {**component, "dns_servers": safe_dns}


class OpenVPNConfiguration(BaseSubscription):
    def __init__(self, *, refuse_unissued_secret: bool = True):
        self.proxy_remarks = []
        self.components: list[dict] = []
        self.refuse_unissued_secret = refuse_unissued_secret

    def add(self, remark: str, address: str, inbound: SubscriptionInboundData, settings: dict):
        component = self._build_openvpn_components(remark, address, inbound, settings)
        if not component:
            return
        safe_component = _safe_component(component)
        if safe_component is None:
            logger.warning(f"Skipping OpenVPN remote for inbound {inbound.inbound_tag}: a value would break the profile")
            return
        self.components.append(safe_component)

    def _files(self) -> dict[str, str]:
        if not self.components:
            return {}

        primary_pki = (self.components[0]["ca_cert"], self.components[0]["tls_crypt_key"])
        matching = [c for c in self.components if (c["ca_cert"], c["tls_crypt_key"]) == primary_pki]

        by_protocol: dict[str, list[dict]] = {}
        for component in matching:
            by_protocol.setdefault(str(component["protocol"]).lower(), []).append(component)

        files = {}
        for protocol, group in by_protocol.items():
            files[f"openvpn-{protocol}.ovpn"] = self._render_one(group)
        return files

    def _render_one(self, components: list[dict]) -> str:
        primary = components[0]
        connection_blocks = [
            "\n".join(
                [
                    "<connection>",
                    f"remote {c['address']} {c['port']} {c['protocol']}",
                    "</connection>",
                ]
            )
            for c in components
        ]
        dns_lines = [f"dhcp-option DNS {dns}" for dns in primary.get("dns_servers") or []]

        tuning = []
        mtus = [c.get("tun_mtu") or 0 for c in components if (c.get("tun_mtu") or 0) > 0]
        if mtus:
            tuning.append(f"tun-mtu {min(mtus)}")
        mss = [c.get("mssfix") or 0 for c in components if (c.get("mssfix") or 0) > 0]
        if mss:
            tuning.append(f"mssfix {min(mss)}")
        if all(str(c.get("protocol", "")).lower() == "udp" for c in components):
            frags = [c.get("fragment") or 0 for c in components if (c.get("fragment") or 0) > 0]
            if frags:
                tuning.append(f"fragment {min(frags)}")
        lines = [
            "client",
            "dev tun",
            "nobind",
            "remote-cert-tls server",
            f"cipher {primary['cipher']}",
            f"auth {primary['auth']}",
            *tuning,
            *dns_lines,
            "verb 3",
            "",
            *connection_blocks,
            "",
            "<ca>",
            primary["ca_cert"].strip(),
            "</ca>",
            "",
            "<tls-crypt>",
            primary["tls_crypt_key"].strip(),
            "</tls-crypt>",
            "",
            "<auth-user-pass>",
            primary["username"],
            primary["password"],
            "</auth-user-pass>",
        ]
        return "\n".join(lines) + "\n"

    def render(self) -> bytes:
        files = self._files()
        if not files:
            self._refuse_unissued_secret("openvpn")
            return b""

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for filename, content in files.items():
                zip_file.writestr(filename, content)
        zip_buffer.seek(0)
        return zip_buffer.getvalue()
