import io
import zipfile

from app.models.subscription import SubscriptionInboundData

from .base import BaseSubscription


class OpenVPNConfiguration(BaseSubscription):
    """OpenVPN subscription format.

    Deliberately produces a downloadable .ovpn file, never a share-link -
    OpenVPNConfiguration is never registered in any protocol_handlers dict
    (links.py/singbox.py/clash.py), so it is impossible for an openvpn host
    to leak into any link-based subscription format by omission alone; this
    is enforced independently in app/subscription/base.py's aggregate-format
    host filtering. This mirrors the requirement that drove the design, not
    a technical limitation - OpenVPN's own .ovpn format is inherently a
    multi-directive file, not a single URI, so a file was always the only
    sensible representation.

    A single OpenVPN process can only listen on one address/port/protocol
    combination, so a panel admin models "several protocols on custom ports"
    as multiple instances inside one CoreConfig (app/core/openvpn.py). Rather
    than one .ovpn per instance (mirroring WireGuardConfiguration's per-host
    ZIP), this renders ONE self-contained .ovpn per user with one
    <connection> block per instance the user has access to - OpenVPN clients
    natively try each block in order and fail over automatically, giving a
    single-file "try UDP, fall back to TCP" experience without any app-side
    logic.

    CA certificate, tls-crypt key, cipher, and auth are file-level (global)
    OpenVPN directives, not settable per <connection> block. In the ordinary
    case all of a user's OpenVPN instances come from the same CoreConfig and
    therefore share identical PKI/cipher/auth, so this is a non-issue. If a
    user somehow has instances spanning multiple different OpenVPN
    CoreConfigs (different PKI), only the first-seen PKI is used for the
    file-level directives, and only instances sharing that exact PKI get a
    <connection> block - instances from a different CoreConfig are silently
    omitted rather than producing a file with a security-relevant mismatch
    between what a <connection> block claims and what CA actually is
    embedded in the file.
    """

    def __init__(self):
        self.proxy_remarks = []
        self.components: list[dict] = []

    def add(self, remark: str, address: str, inbound: SubscriptionInboundData, settings: dict):
        component = self._build_openvpn_components(remark, address, inbound, settings)
        if not component:
            return
        self.components.append(component)

    def _files(self) -> dict[str, str]:
        """One .ovpn per L4 protocol, each carrying every remote that speaks it.

        A user's OpenVPN access typically spans a direct instance and a
        tunnelled one that reaches the same server through a relay. Both are
        the same protocol, so they belong in ONE file as two <connection>
        blocks - the client tries them in order and fails over on its own.
        Splitting per protocol instead of per instance is what makes "one TCP
        config, one UDP config" possible while still offering both routes.
        """
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

        # tun-mtu, fragment and mssfix are a contract between the two ends: a
        # server running them against a client that is not will drop or refuse
        # traffic outright. They are file-level directives, so the smallest
        # value among this file's remotes wins - the file has to survive its
        # most constrained path. fragment is UDP-only; OpenVPN rejects it on a
        # TCP config, so it is emitted only when every remote here is UDP.
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
            return b""

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for filename, content in files.items():
                zip_file.writestr(filename, content)
        zip_buffer.seek(0)
        return zip_buffer.getvalue()
