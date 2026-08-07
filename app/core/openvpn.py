from __future__ import annotations

import ipaddress
import json
import os
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import PosixPath

import commentjson
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.models.core import CoreType
from app.models.protocol import ProxyProtocol

_OPENVPN_PROTOCOLS = ("udp", "tcp")
_REQUIRED_PKI_FIELDS = ("ca_cert", "server_cert", "server_key", "tls_crypt_key")


def _protocols_from_inbounds_by_tag(inbounds_by_tag: dict[str, dict]) -> frozenset[ProxyProtocol]:
    return frozenset(
        protocol
        for inbound in inbounds_by_tag.values()
        if (protocol := ProxyProtocol.from_value(inbound["protocol"])) is not None
    )


def _generate_tls_crypt_key() -> str:
    """Generate an OpenVPN static key (tls-crypt), matching the exact format
    `openvpn --genkey secret` produces: 256 random bytes, hex-encoded as 16
    lines of 32 hex characters each, wrapped in the standard armor.
    """
    raw = os.urandom(256)
    hex_str = raw.hex()
    lines = [hex_str[i : i + 32] for i in range(0, len(hex_str), 32)]
    body = "\n".join(lines)
    return f"-----BEGIN OpenVPN Static key V1-----\n{body}\n-----END OpenVPN Static key V1-----"


def generate_openvpn_pki(common_name: str = "PasarGuard-OpenVPN-CA") -> dict[str, str]:
    """Generate a self-signed CA + server certificate/key + tls-crypt static
    key for an OpenVPN CoreConfig.

    Unlike Hysteria2's TLS certificate (which faces the public internet and
    benefits from being a real, publicly-trusted certificate for anti-DPI
    blending), OpenVPN's CA never needs public trust - it is embedded
    directly in every user's downloaded .ovpn file (verify-client-cert none
    means clients never present their own certificate, but they DO validate
    the server's certificate against this same embedded CA). A self-signed
    internal CA is the standard, expected setup for this reason - this
    mirrors what easy-rsa produces for a typical OpenVPN deployment.

    Generated once per CoreConfig and stored inline as PEM/armored text in
    CoreConfig.config["pki"] - the same "generate once, store inline" pattern
    already used for the Hysteria2 TLS certificate in app/core/singbox.py.
    """
    now = datetime.now(UTC)

    ca_key = ec.generate_private_key(ec.SECP384R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA384())
    )

    server_key = ec.generate_private_key(ec.SECP384R1())
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "server")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                key_agreement=True,
                content_commitment=False,
                data_encipherment=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA384())
    )

    return {
        "ca_cert": ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
        "server_cert": server_cert.public_bytes(serialization.Encoding.PEM).decode(),
        "server_key": server_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        "tls_crypt_key": _generate_tls_crypt_key(),
    }


class OpenVPNConfig(dict):
    """AbstractCore implementation for OpenVPN.

    Unlike Xray/sing-box (per-connection proxy cores with one process and
    many inbounds), OpenVPN has no single-process multi-listener mode - each
    configured "instance" (a protocol+port combination, e.g. UDP/1194 and
    TCP/443) is rendered and run as its own independent `openvpn` subprocess
    by the node's backend/openvpn Go package. What XRayConfig/SingBoxConfig
    call "inbounds" this class calls "instances" internally, but exposes
    through the same AbstractCore `inbounds`/`inbounds_by_tag` properties so
    the rest of the panel (Host <-> inbound_tag resolution, subscription
    building) works unmodified - each instance tag is just another inbound
    tag from that code's point of view.

    PKI (CA + server cert/key + tls-crypt static key) is server-side only -
    OpenVPN is configured with `verify-client-cert none` +
    `management-client-auth`, so there are no per-user client certificates,
    only a username/password per user (see app/models/proxy.py
    OpenVPNSettings). PKI is generated once via generate_openvpn_pki() and
    must already be present in the config dict by the time this class
    validates it - construction here is side-effect-free, matching how
    SingBoxConfig expects an already-provisioned TLS certificate rather than
    generating one itself.
    """

    def __init__(
        self,
        config: dict | str | PosixPath | None = None,
        exclude_inbound_tags: set[str] | None = None,
        fallbacks_inbound_tags: set[str] | None = None,
        skip_validation: bool = False,
    ):
        if config is None:
            config = {}
        if isinstance(config, str):
            config = commentjson.loads(config)
        if isinstance(config, dict):
            config = deepcopy(config)

        super().__init__(config)

        self._type = CoreType.openvpn
        self.exclude_inbound_tags = set(exclude_inbound_tags or set())
        self.fallbacks_inbound_tags = set(fallbacks_inbound_tags or set())
        self._inbounds: list[str] = []
        self._inbounds_by_tag: dict[str, dict] = {}
        self._protocols: frozenset[ProxyProtocol] = frozenset()

        if skip_validation:
            return

        self._validate()
        self._resolve_instances()

    @property
    def type(self) -> str:
        return self._type

    def _validate(self):
        if self.fallbacks_inbound_tags:
            raise ValueError("fallbacks_inbound_tags is not supported for OpenVPN cores")

        instances = self.get("instances")
        if not instances:
            raise ValueError("config doesn't have instances")
        if not isinstance(instances, list):
            raise TypeError("instances must be a list")

        pki = self.get("pki")
        if not isinstance(pki, dict):
            raise TypeError("config doesn't have a pki section")
        for field in _REQUIRED_PKI_FIELDS:
            if not pki.get(field):
                raise ValueError(f"pki.{field} is required - generate it via generate_openvpn_pki() before saving")

        seen_tags: set[str] = set()
        seen_ports: set[tuple[str, int]] = set()
        for instance in instances:
            self._validate_instance(instance, seen_tags, seen_ports)

    def _validate_instance(self, instance: dict, seen_tags: set[str], seen_ports: set[tuple[str, int]]):
        tag = instance.get("tag")
        if not tag:
            raise ValueError("all instances must have a unique tag")
        if tag in seen_tags:
            raise ValueError(f"duplicate instance tag: {tag}")
        seen_tags.add(tag)

        protocol = instance.get("protocol")
        if protocol not in _OPENVPN_PROTOCOLS:
            raise ValueError(f"{tag}: protocol must be one of {_OPENVPN_PROTOCOLS}, got {protocol!r}")

        port = instance.get("port")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ValueError(f"{tag}: port must be an integer between 1 and 65535")
        port_key = (protocol, port)
        if port_key in seen_ports:
            raise ValueError(f"{tag}: duplicate {protocol}/{port} within this core config")
        seen_ports.add(port_key)

        network = instance.get("network")
        if not network:
            raise ValueError(f"{tag}: network (CIDR) is required")
        try:
            ipaddress.ip_network(network, strict=False)
        except ValueError as exc:
            raise ValueError(f"{tag}: network must be a valid CIDR, got {network!r}") from exc

        max_clients = instance.get("max_clients")
        if max_clients is not None and (not isinstance(max_clients, int) or max_clients < 1):
            raise ValueError(f"{tag}: max_clients must be a positive integer")

    def _resolve_instances(self):
        for instance in self["instances"]:
            self._read_instance(instance)
        self._protocols = _protocols_from_inbounds_by_tag(self._inbounds_by_tag)

    def _read_instance(self, instance: dict):
        tag = instance["tag"]
        if tag in self.exclude_inbound_tags:
            return

        pki = self.get("pki", {})
        metadata = {
            "tag": tag,
            "protocol": "openvpn",
            "network": instance["protocol"],  # udp/tcp - the actual L4 transport
            "tls": "tls",
            "port": instance.get("port"),
            "sni": "",
            # Generic per-inbound extra-data channel (same mechanism Hysteria2
            # obfs/quicParams already use via app.core.hosts) - carries
            # everything _build_openvpn_components (app/subscription/base.py)
            # needs to render a self-contained .ovpn <connection> block that a
            # bare host/port/proto alone can't express.
            "finalmask": {
                "openvpn": {
                    "cipher": instance.get("cipher", "AES-256-GCM"),
                    "auth": instance.get("auth", "SHA256"),
                    "dns_servers": instance.get("dns_servers", []),
                    "redirect_gateway": bool(instance.get("redirect_gateway", True)),
                    "ca_cert": pki.get("ca_cert", ""),
                    "tls_crypt_key": pki.get("tls_crypt_key", ""),
                }
            },
        }

        self._inbounds.append(tag)
        self._inbounds_by_tag[tag] = metadata

    def to_str(self, **json_kwargs) -> str:
        return json.dumps(self, **json_kwargs)

    @property
    def inbounds_by_tag(self) -> dict:
        return self._inbounds_by_tag

    @property
    def inbounds(self) -> list[str]:
        return self._inbounds

    @property
    def protocols(self) -> frozenset[ProxyProtocol]:
        return self._protocols

    @property
    def instances(self) -> list[dict]:
        return self.get("instances", [])

    def to_json(self) -> dict:
        return {
            "type": self.type,
            "config": dict(self),
            "exclude_inbound_tags": list(self.exclude_inbound_tags),
            "fallbacks_inbound_tags": [],
            "inbounds": self.inbounds,
            "inbounds_by_tag": self.inbounds_by_tag,
        }

    @classmethod
    def from_json(cls, data: dict) -> OpenVPNConfig:
        instance = cls(
            config=data.get("config", {}),
            exclude_inbound_tags=set(data.get("exclude_inbound_tags", [])),
            skip_validation=True,
        )
        if "inbounds" in data:
            instance._inbounds = data["inbounds"]
        if "inbounds_by_tag" in data:
            instance._inbounds_by_tag = data["inbounds_by_tag"]
        instance._protocols = _protocols_from_inbounds_by_tag(instance._inbounds_by_tag)
        return instance

    def copy(self):
        return deepcopy(self)
