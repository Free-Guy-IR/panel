import hashlib
import time
from urllib.parse import quote

from fastapi import HTTPException

from app.models.subscription import SubscriptionInboundData
from app.utils.logger import get_logger

logger = get_logger("subscription")

UNISSUED_SECRET_LABELS = {"openvpn": "OpenVPN", "l2tp": "L2TP", "mtproto": "MTProto"}
UNISSUED_WARNING_INTERVAL_SECONDS = 3600.0
UNISSUED_WARNING_MEMORY = 10_000
_unissued_warned_at: dict[tuple[object, str], float] = {}


def _warn_unissued_secret(protocol: str, user_id, inbound_tag: str) -> None:
    now = time.monotonic()
    key = (user_id, protocol)
    last = _unissued_warned_at.get(key)
    if last is not None and now - last < UNISSUED_WARNING_INTERVAL_SECONDS:
        return
    _unissued_warned_at[key] = now
    if len(_unissued_warned_at) > UNISSUED_WARNING_MEMORY:
        for stale in [k for k, seen in _unissued_warned_at.items() if now - seen >= UNISSUED_WARNING_INTERVAL_SECONDS]:
            del _unissued_warned_at[stale]
    logger.warning(
        f"user {user_id} can reach {protocol} inbound {inbound_tag!r} but holds no {protocol} secret, "
        "so nothing was rendered for it; run the entitled activation for this protocol"
    )


class UnissuedSecretError(HTTPException):
    def __init__(self, protocol: str):
        self.protocol = protocol
        label = UNISSUED_SECRET_LABELS.get(protocol, protocol)
        super().__init__(
            status_code=409,
            detail=f"{label} has not been activated for this user yet; ask the administrator to activate it",
        )


def _pick_fake_tls_domain(domains: list[str], key: str) -> str:
    if len(domains) == 1:
        return domains[0]

    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return domains[int.from_bytes(digest[:8], "big") % len(domains)]


class ForkSubscriptionHelpers:
    refuse_unissued_secret: bool = False
    unissued_secret_protocols: frozenset[str] = frozenset()

    def _note_unissued_secret(self, protocol: str, user_id, inbound: SubscriptionInboundData) -> None:
        if inbound.protocol != protocol:
            return
        self.unissued_secret_protocols = self.unissued_secret_protocols | {protocol}
        _warn_unissued_secret(protocol, user_id, inbound.inbound_tag)

    def _refuse_unissued_secret(self, protocol: str) -> None:
        if self.refuse_unissued_secret and protocol in self.unissued_secret_protocols:
            raise UnissuedSecretError(protocol)

    def _build_openvpn_components(
        self, remark: str, address: str, inbound: SubscriptionInboundData, settings: dict
    ) -> dict | None:
        """Build one <connection> block worth of data for an OpenVPN instance.

        Unlike every proxy-style protocol here, OpenVPN has no share-link URI
        at all - by design (see app/subscription/openvpn.py), this only ever
        feeds the downloadable .ovpn file builder. Reads instance config
        (cipher/auth/dns/PKI) from inbound.finalmask["openvpn"], the same
        generic per-inbound extra-data channel Hysteria2 obfs/quicParams
        already use, populated by app.core.openvpn.OpenVPNConfig._read_instance.
        """
        user_id = settings.get("_user_id")
        password = settings.get("password")
        if user_id is None:
            return None
        if not password:
            self._note_unissued_secret("openvpn", user_id, inbound)
            return None
        username = str(user_id)  # matches app.node.user._serialize_user_for_node's str(id) convention

        finalmask = inbound.finalmask
        if finalmask is None:
            finalmask_dict = {}
        elif isinstance(finalmask, dict):
            finalmask_dict = finalmask
        else:
            # FinalMask is a real Pydantic model (extra="allow"), not a plain
            # dict - model_dump() is what surfaces the "openvpn" extra field
            # OpenVPNConfig._read_instance attaches, alongside its normal
            # tcp/udp/quicParams fields.
            finalmask_dict = finalmask.model_dump(by_alias=True, exclude_none=True)
        ovpn_data = finalmask_dict.get("openvpn", {})
        ca_cert = ovpn_data.get("ca_cert")
        tls_crypt_key = ovpn_data.get("tls_crypt_key")
        if not ca_cert or not tls_crypt_key:
            return None

        # Host-level DNS override (set on this specific Host's Network
        # Settings) takes priority over the CoreConfig instance's own
        # default dns_servers; falls back to the core default when the host
        # has none set.
        dns_servers = inbound.openvpn_dns_servers if inbound.openvpn_dns_servers else ovpn_data.get("dns_servers", [])

        return {
            "remark": self._remark_validation(remark),
            "address": address,
            "port": inbound.port,
            "protocol": inbound.network,  # udp/tcp, set as the L4 transport by OpenVPNConfig
            "tun_mtu": int(ovpn_data.get("tun_mtu") or 0),
            "fragment": int(ovpn_data.get("fragment") or 0),
            "mssfix": int(ovpn_data.get("mssfix") or 0),
            "username": username,
            "password": password,
            "cipher": ovpn_data.get("cipher", "AES-256-GCM"),
            "auth": ovpn_data.get("auth", "SHA256"),
            "dns_servers": dns_servers,
            "ca_cert": ca_cert,
            "tls_crypt_key": tls_crypt_key,
        }

    def _build_l2tp_components(
        self, remark: str, address: str, inbound: SubscriptionInboundData, settings: dict
    ) -> dict | None:
        user_id = settings.get("_user_id")
        password = settings.get("password")
        if user_id is None:
            return None
        if not password:
            self._note_unissued_secret("l2tp", user_id, inbound)
            return None

        finalmask = inbound.finalmask
        if finalmask is None:
            finalmask_dict = {}
        elif isinstance(finalmask, dict):
            finalmask_dict = finalmask
        else:
            finalmask_dict = finalmask.model_dump(by_alias=True, exclude_none=True)
        l2tp_data = finalmask_dict.get("l2tp") or {}
        psk = l2tp_data.get("psk")
        if not psk:
            return None

        server = (address or "").strip() or str(l2tp_data.get("server_addr") or "").strip()
        if not server:
            return None

        return {
            "remark": self._remark_validation(remark),
            "server": server,
            "username": str(user_id),
            "password": str(password),
            "secret": str(psk),
            "dns": [str(d) for d in (l2tp_data.get("dns") or [])],
        }

    def _build_mtproto_components(
        self, remark: str, address: str, inbound: SubscriptionInboundData, settings: dict
    ) -> dict | None:
        """Build a tg://proxy?... link for one MTProto instance.

        Unlike OpenVPN, MTProto links ARE meant to appear in the standard
        link aggregator (see app/subscription/links.py's protocol_handlers) -
        tg:// is itself already a shareable single-URI scheme, no file
        download needed.

        The stored secret (settings["secret"], from ProxyTable.mtproto.secret)
        is just the raw 16-byte key, hex-encoded - the client-facing secret
        Telegram apps expect also embeds the fake-TLS domain (see the mtg
        fork's mtglib/secret.go Secret.Hex(): 0xee prefix + key bytes + ASCII
        hostname, all hex-encoded). Built here rather than stored, since the
        domain is an instance/host-level property, not a per-user one.
        """
        user_id = settings.get("_user_id")
        raw_secret = settings.get("secret")
        if user_id is None:
            return None
        if not raw_secret:
            self._note_unissued_secret("mtproto", user_id, inbound)
            return None

        finalmask = inbound.finalmask
        if finalmask is None:
            finalmask_dict = {}
        elif isinstance(finalmask, dict):
            finalmask_dict = finalmask
        else:
            finalmask_dict = finalmask.model_dump(by_alias=True, exclude_none=True)
        mtproto_data = finalmask_dict.get("mtproto", {})
        mode = mtproto_data.get("mode") or "faketls"

        if mode == "plain":
            full_secret = "dd" + raw_secret
        else:
            domains = mtproto_data.get("fake_tls_domains") or []
            if not domains:
                single = mtproto_data.get("fake_tls_domain")
                domains = [single] if single else []
            if not domains:
                return None

            domain = _pick_fake_tls_domain(domains, str(user_id))
            full_secret = "ee" + raw_secret + domain.encode("ascii").hex()

        validated_remark = self._remark_validation(remark)
        self.proxy_remarks.append(validated_remark)

        return {
            "remark": validated_remark,
            "address": address,
            "port": inbound.port,
            "secret": full_secret,
            "uri": f"tg://proxy?server={address}&port={inbound.port}&secret={full_secret}#{quote(validated_remark)}",
        }

    def _build_mtproto(self, remark: str, address: str, inbound: SubscriptionInboundData, settings: dict) -> str:
        components = self._build_mtproto_components(remark, address, inbound, settings)
        if not components:
            return ""
        return components["uri"]
