from __future__ import annotations

import json
import re
from copy import deepcopy
from ipaddress import ip_address, ip_network
from pathlib import PosixPath

import commentjson

from app.models.core import CoreType
from app.models.protocol import ProxyProtocol

L2TP_PORT = 1701
PSK_MIN_LENGTH = 8
PSK_MAX_LENGTH = 128
DEFAULT_DNS = ["1.1.1.1", "8.8.8.8"]

_L2TP_PROTOCOLS = frozenset((ProxyProtocol.l2tp,))
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PROPOSAL_RE = re.compile(r"^[a-z0-9-]+$")
_IFACE_RE = re.compile(r"^[A-Za-z0-9._@-]{1,15}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_PSK_FORBIDDEN = frozenset('"\\#{}')


def _required_str(key: str, value) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _optional_str(key: str, value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value.strip()


def validate_psk(psk: str) -> str:
    psk = _required_str("psk", psk)
    if not PSK_MIN_LENGTH <= len(psk) <= PSK_MAX_LENGTH:
        raise ValueError(f"psk must be between {PSK_MIN_LENGTH} and {PSK_MAX_LENGTH} characters")
    for ch in psk:
        if not (" " < ch <= "~"):
            raise ValueError("psk must contain only printable ASCII characters without spaces")
        if ch in _PSK_FORBIDDEN:
            raise ValueError(f"psk must not contain {ch!r}")
    return psk


def validate_server_addr(value: str) -> str:
    value = _required_str("server_addr", value).rstrip(".")
    if not value:
        raise ValueError("server_addr is required (the public IP or hostname clients connect to)")
    try:
        ip_address(value)
        return value
    except ValueError:
        pass
    if not _HOSTNAME_RE.fullmatch(value):
        raise ValueError("server_addr must be an IP address or a hostname")
    return value


def _validate_proposals(key: str, value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TypeError(f"{key} must be a list of strings")
    out = []
    for item in value:
        item = item.strip().lower()
        if not item:
            continue
        if not _PROPOSAL_RE.fullmatch(item):
            raise ValueError(f"{key} entry {item!r} contains characters strongSwan does not accept")
        out.append(item)
    return out


class L2TPConfig(dict):
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

        self._type = CoreType.l2tp
        self.exclude_inbound_tags = set(exclude_inbound_tags or set())
        self.fallbacks_inbound_tags = set(fallbacks_inbound_tags or set())
        self._inbounds: list[str] = []
        self._inbounds_by_tag: dict[str, dict] = {}

        if skip_validation:
            return

        self._validate()
        self._resolve_inbounds()

    @property
    def type(self) -> str:
        return self._type

    def _validate(self):
        if self.exclude_inbound_tags:
            raise ValueError("exclude_inbound_tags is not supported for L2TP cores")
        if self.fallbacks_inbound_tags:
            raise ValueError("fallbacks_inbound_tags is not supported for L2TP cores")

        inbound_tag = _required_str("inbound_tag", self.get("inbound_tag"))
        if not _TAG_RE.fullmatch(inbound_tag):
            raise ValueError(
                "inbound_tag must start with a letter or digit and contain only letters, digits, '_', '.', '-' (max 64)"
            )
        self["inbound_tag"] = inbound_tag

        self["server_addr"] = validate_server_addr(self.get("server_addr"))

        pool_raw = _required_str("pool", self.get("pool"))
        try:
            pool = ip_network(pool_raw, strict=False)
        except ValueError as exc:
            raise ValueError(f"pool {pool_raw!r} is not a valid CIDR") from exc
        if pool.version != 4:
            raise ValueError("pool must be an IPv4 network")
        if not 8 <= pool.prefixlen <= 29:
            raise ValueError("pool prefix must be between /8 and /29")
        self["pool"] = str(pool)

        local_ip_raw = _optional_str("local_ip", self.get("local_ip"))
        if not local_ip_raw:
            local_ip_raw = str(pool.network_address + 1)
        try:
            local_ip = ip_address(local_ip_raw)
        except ValueError as exc:
            raise ValueError("local_ip must be an IPv4 address") from exc
        if local_ip.version != 4 or local_ip not in pool:
            raise ValueError(f"local_ip must be an IPv4 address inside the pool {pool}")
        self["local_ip"] = str(local_ip)

        egress = _optional_str("egress_interface", self.get("egress_interface"))
        if egress and not _IFACE_RE.fullmatch(egress):
            raise ValueError("egress_interface must be a valid interface name (max 15 chars)")
        self["egress_interface"] = egress

        dns = self.get("dns")
        if dns is None or dns == []:
            dns = list(DEFAULT_DNS)
        if not isinstance(dns, list) or not all(isinstance(d, str) for d in dns):
            raise TypeError("dns must be a list of strings")
        cleaned_dns = []
        for entry in dns:
            entry = entry.strip()
            try:
                parsed = ip_address(entry)
            except ValueError as exc:
                raise ValueError(f"dns entry {entry!r} must be an IPv4 address") from exc
            if parsed.version != 4:
                raise ValueError(f"dns entry {entry!r} must be an IPv4 address")
            cleaned_dns.append(str(parsed))
        self["dns"] = cleaned_dns

        self["ike_proposals"] = _validate_proposals("ike_proposals", self.get("ike_proposals"))
        self["esp_proposals"] = _validate_proposals("esp_proposals", self.get("esp_proposals"))
        legacy = self.get("legacy_clients", False)
        if not isinstance(legacy, bool):
            raise TypeError("legacy_clients must be a boolean")
        self["legacy_clients"] = legacy

        self["psk"] = validate_psk(self.get("psk"))

    def _resolve_inbounds(self):
        inbound_tag = self["inbound_tag"]
        metadata = {
            "tag": inbound_tag,
            "protocol": "l2tp",
            "network": "udp",
            "tls": "none",
            "port": L2TP_PORT,
            "finalmask": {
                "l2tp": {
                    "server_addr": self["server_addr"],
                    "psk": self["psk"],
                    "dns": list(self["dns"]),
                }
            },
        }
        self._inbounds = [inbound_tag]
        self._inbounds_by_tag = {inbound_tag: metadata}

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
        return _L2TP_PROTOCOLS

    @property
    def psk(self) -> str:
        return self["psk"]

    def to_json(self) -> dict:
        return {
            "type": self.type,
            "config": dict(self),
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
            "inbounds": self.inbounds,
            "inbounds_by_tag": self.inbounds_by_tag,
        }

    @classmethod
    def from_json(cls, data: dict) -> L2TPConfig:
        return cls(config=data.get("config", {}))

    def copy(self):
        return deepcopy(self)
