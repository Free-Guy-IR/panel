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

    def render(self) -> bytes:
        if not self.components:
            return b""

        primary = self.components[0]
        primary_pki = (primary["ca_cert"], primary["tls_crypt_key"])
        matching = [c for c in self.components if (c["ca_cert"], c["tls_crypt_key"]) == primary_pki]

        connection_blocks = []
        for component in matching:
            connection_blocks.append(
                "\n".join(
                    [
                        "<connection>",
                        f"remote {component['address']} {component['port']} {component['protocol']}",
                        "</connection>",
                    ]
                )
            )

        lines = [
            "client",
            "dev tun",
            "nobind",
            "remote-cert-tls server",
            f"cipher {primary['cipher']}",
            f"auth {primary['auth']}",
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
            # <auth-user-pass> inline is rejected by the classic openvpn2 CLI
            # ("option 'auth-user-pass' is not expected to be inline"), but
            # openvpn3 - the core used by OpenVPN Connect, the client almost
            # every real user (especially on mobile) actually imports this
            # file into - does support it and connects with zero prompts,
            # verified with a live `openvpn3 session-start` against this
            # exact renderer's output. openvpn2-CLI users are a small
            # minority for this deployment, so this optimizes for the
            # zero-touch import experience over openvpn2-CLI compatibility.
            "<auth-user-pass>",
            primary["username"],
            primary["password"],
            "</auth-user-pass>",
        ]

        return ("\n".join(lines) + "\n").encode()
