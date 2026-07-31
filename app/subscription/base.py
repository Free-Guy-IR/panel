import base64
import hashlib
import json
import re
from enum import Enum
from typing import Any, Literal
from urllib.parse import quote, urlencode

from app.models.subscription import SubscriptionInboundData


class BaseSubscription:
    def __init__(
        self,
        user_agent_template_content: str | None = None,
        grpc_user_agent_template_content: str | None = None,
    ):
        self.proxy_remarks = []
        user_agent_data = json.loads(user_agent_template_content) if user_agent_template_content else {}
        if "list" in user_agent_data and isinstance(user_agent_data["list"], list):
            self.user_agent_list = user_agent_data["list"]
        else:
            self.user_agent_list = []

        grpc_user_agent_data = json.loads(grpc_user_agent_template_content) if grpc_user_agent_template_content else {}

        if "list" in grpc_user_agent_data and isinstance(grpc_user_agent_data["list"], list):
            self.grpc_user_agent_data = grpc_user_agent_data["list"]
        else:
            self.grpc_user_agent_data = []

        del user_agent_data, grpc_user_agent_data

    def _remark_validation(self, remark):
        if remark not in self.proxy_remarks:
            return remark
        c = 2
        while True:
            new = f"{remark} ({c})"
            if new not in self.proxy_remarks:
                return new
            c += 1

    def _normalize_and_remove_none_values(self, data: dict) -> dict:
        """
        Clean dictionary by removing None, empty strings, and 0 values.
        Converts Enum values and recursively cleans nested dictionaries.

        Args:
            data: Input dictionary to clean

        Returns:
            Cleaned dictionary with empty values removed
        """

        def clean_dict(d: dict) -> dict:
            new_dict = {}
            for k, v in d.items():
                if v not in (None, "", 0):
                    if isinstance(v, dict):
                        if cleaned_dict := clean_dict(v):
                            new_dict[k] = cleaned_dict
                    else:
                        if isinstance(v, Enum):
                            new_dict[k] = v.value
                        else:
                            new_dict[k] = v
            return new_dict

        return clean_dict(data)

    def snake_to_camel(self, snake_str):
        return re.sub(r"_([a-z])", lambda match: match.group(1).upper(), snake_str)

    @staticmethod
    def get_grpc_gun(path: str) -> str:
        """Extract gRPC gun service name from path"""
        if not path.startswith("/"):
            return path

        servicename = path.rsplit("/", 1)[0]
        streamname = path.rsplit("/", 1)[1].split("|")[0]

        if streamname == "Tun":
            return servicename[1:]

        return f"{servicename}/{streamname}"

    @staticmethod
    def get_grpc_multi(path: str) -> str:
        """Extract gRPC multi service name from path"""
        if not path.startswith("/"):
            return path

        servicename = path.rsplit("/", 1)[0]
        streamname = path.rsplit("/", 1)[1].split("|")[1]

        return f"{servicename}/{streamname}"

    @staticmethod
    def ensure_base64_password(password: str, method: str) -> str:
        """
        Ensure password is base64 encoded with correct length for the method:
        - aes-128-gcm: 16 bytes key (22 chars in base64)
        - aes-256-gcm and chacha20-poly1305: 32 bytes key (44 chars in base64)
        """
        try:
            # Check if it's already a valid base64 string
            decoded_bytes = base64.b64decode(password)
            # Check if length is appropriate
            if ("aes-128-gcm" in method and len(decoded_bytes) == 16) or (
                ("aes-256-gcm" in method or "chacha20-poly1305" in method) and len(decoded_bytes) == 32
            ):
                # Already correct length
                return password
        except Exception:
            # Not a valid base64 string
            pass

        # Hash the password to get a consistent byte array
        hash_bytes = hashlib.sha256(password.encode("utf-8")).digest()

        if "aes-128-gcm" in method:
            key_bytes = hash_bytes[:16]  # First 16 bytes for AES-128
        else:
            key_bytes = hash_bytes[:32]  # First 32 bytes for AES-256 or ChaCha20

        return base64.b64encode(key_bytes).decode("ascii")

    @staticmethod
    def password_to_2022(inbound_password: str, user_password: str, method: str) -> str:
        """
        Convert a password to the format required for 2022-blake3 methods,
        ensuring correct key length.
        """
        base64_string = BaseSubscription.ensure_base64_password(user_password, method)
        return f"{inbound_password}:{base64_string}"

    @staticmethod
    def detect_shadowsocks_2022(
        is_2022: bool, inbound_method: str, user_method: str, inbound_password: str, user_password: str
    ) -> tuple[str, str]:
        """Detect and handle Shadowsocks 2022 password format"""
        if is_2022:
            password = BaseSubscription.password_to_2022(inbound_password, user_password, inbound_method)
            method = inbound_method
        else:
            password = user_password
            method = user_method
        return method, password

    @staticmethod
    def vless_route(uuid: str, route: str) -> str:
        """
        Replace a third part of a UUID with a custom value.

        Args:
            uuid: The UUID as a string or uuid.UUID object
            route: The value to replace the third part

        Returns:
            The modified UUID as a string with vless route

        Example:
            >>> original = "c90cff8e-d651-414e-8e83-1a187622d957"
            >>> result = vless_route(original, "9999")
            >>> print(result)
            "c90cff8e-d651-9999-8e83-1a187622d957"
        """

        parts = uuid.split("-")
        parts[2] = route
        return "-".join(parts)

    def _get_hysteria_data_from_finalmask(self, finalmask: dict | None) -> tuple[Any | Literal[""], Any | dict]:
        """Extract Hysteria obfuscation password and QUIC parameters from finalmask"""

        if finalmask is None:
            finalmask = {}
        obfs_password = ""
        quic_params: dict = finalmask.get("quicParams", {})
        if udp := finalmask.get("udp"):
            for i in udp:
                if i.get("type") == "salamander":
                    obfs_password = i.get("settings", {}).get("password")
                    break

        return obfs_password, quic_params

    def _build_wireguard_components(
        self, remark: str, address: str, inbound: SubscriptionInboundData, settings: dict
    ) -> dict | None:
        private_key = settings.get("private_key", "")
        peer_ips = list(settings.get("peer_ips") or [])
        public_key = inbound.wireguard_public_key
        if not private_key or not peer_ips or not public_key:
            return None

        validated_remark = self._remark_validation(remark)
        self.proxy_remarks.append(validated_remark)

        payload = {
            "publickey": public_key,
            "address": ",".join(peer_ips),
        }

        if inbound.wireguard_mtu:
            payload["mtu"] = inbound.wireguard_mtu
        if inbound.wireguard_allowed_ips:
            payload["allowedips"] = ",".join(inbound.wireguard_allowed_ips)
        if inbound.wireguard_keepalive:
            payload["keepalive"] = inbound.wireguard_keepalive
        if inbound.wireguard_reserved:
            payload["reserved"] = inbound.wireguard_reserved
        if inbound.wireguard_dns:
            payload["dns"] = ",".join(inbound.wireguard_dns)
        if inbound.wireguard_pre_shared_key:
            payload["presharedkey"] = inbound.wireguard_pre_shared_key
        if inbound.finalmask_link:
            payload["fm"] = inbound.finalmask_link

        payload = self._normalize_and_remove_none_values(payload)
        uri_payload = dict(payload)

        return {
            "remark": validated_remark,
            "private_key": private_key,
            "peer_ips": peer_ips,
            "payload": payload,
            "uri": (
                f"wireguard://{quote(private_key, safe='')}@{address}:{inbound.port}/"
                f"?{urlencode(uri_payload, quote_via=quote)}#{quote(validated_remark)}"
            ),
        }

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
        if user_id is None or not password:
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
            "username": username,
            "password": password,
            "cipher": ovpn_data.get("cipher", "AES-256-GCM"),
            "auth": ovpn_data.get("auth", "SHA256"),
            "dns_servers": dns_servers,
            "ca_cert": ca_cert,
            "tls_crypt_key": tls_crypt_key,
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
        if user_id is None or not raw_secret:
            return None

        finalmask = inbound.finalmask
        if finalmask is None:
            finalmask_dict = {}
        elif isinstance(finalmask, dict):
            finalmask_dict = finalmask
        else:
            finalmask_dict = finalmask.model_dump(by_alias=True, exclude_none=True)
        mtproto_data = finalmask_dict.get("mtproto", {})
        fake_tls_domain = mtproto_data.get("fake_tls_domain")
        if not fake_tls_domain:
            return None

        full_secret = "ee" + raw_secret + fake_tls_domain.encode("ascii").hex()

        validated_remark = self._remark_validation(remark)
        self.proxy_remarks.append(validated_remark)

        return {
            "remark": validated_remark,
            "address": address,
            "port": inbound.port,
            "secret": full_secret,
            "uri": f"tg://proxy?server={address}&port={inbound.port}&secret={full_secret}",
        }
