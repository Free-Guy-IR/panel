from __future__ import annotations

import json
from copy import deepcopy
from pathlib import PosixPath
from typing import Union

import commentjson

from app.models.core import CoreType
from app.models.protocol import ProxyProtocol


def _protocols_from_inbounds_by_tag(inbounds_by_tag: dict[str, dict]) -> frozenset[ProxyProtocol]:
    return frozenset(
        protocol
        for inbound in inbounds_by_tag.values()
        if (protocol := ProxyProtocol.from_value(inbound["protocol"])) is not None
    )


class MTProtoConfig(dict):
    """AbstractCore implementation for MTProto (Telegram proxy).

    Unlike OpenVPN (one subprocess per instance) or a naive per-user-process
    design, MTProto instances run as one shared in-process mtglib.Proxy each
    on the node (see backend/mtproto in the node repo) - many users
    multiplexed by their individual secret against a single listener, the
    same "one process, N users" shape Xray/sing-box use. This is only
    possible because the node runs a fork of the mtg proxy library
    (github.com/Free-Guy-IR/mtg) patched to accept many simultaneous secrets
    and report per-secret traffic - the official/upstream MTProto proxy
    implementations report only whole-process aggregate byte counters, with
    no per-user breakdown at all.

    Each configured "instance" is a fake-TLS listener: a port plus the
    domain-fronting host every connection is validated against (and that
    unauthenticated connections are proxied to, for probe resistance) - see
    the node repo's backend/mtproto/config.go for the exact same shape this
    mirrors. There is no PKI (MTProto secrets are symmetric, not
    certificate-based) and no protocol/network choice (mtg is TCP-only) -
    this is why this class's config validation is much smaller than
    OpenVPNConfig's.
    """

    def __init__(
        self,
        config: Union[dict, str, PosixPath] | None = None,
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

        self._type = CoreType.mtproto
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
            raise ValueError("fallbacks_inbound_tags is not supported for MTProto cores")

        instances = self.get("instances")
        if not instances:
            raise ValueError("config doesn't have instances")
        if not isinstance(instances, list):
            raise ValueError("instances must be a list")

        seen_tags: set[str] = set()
        seen_ports: set[int] = set()
        for instance in instances:
            self._validate_instance(instance, seen_tags, seen_ports)

    def _validate_instance(self, instance: dict, seen_tags: set[str], seen_ports: set[int]):
        tag = instance.get("tag")
        if not tag:
            raise ValueError("all instances must have a unique tag")
        if tag in seen_tags:
            raise ValueError(f"duplicate instance tag: {tag}")
        seen_tags.add(tag)

        port = instance.get("port")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            raise ValueError(f"{tag}: port must be an integer between 1 and 65535")
        if port in seen_ports:
            raise ValueError(f"{tag}: duplicate port {port} within this core config")
        seen_ports.add(port)

        fake_tls_domain = instance.get("fake_tls_domain")
        if not fake_tls_domain or not isinstance(fake_tls_domain, str):
            raise ValueError(f"{tag}: fake_tls_domain is required")

        ad_tag = instance.get("ad_tag")
        if ad_tag:
            if not isinstance(ad_tag, str):
                raise ValueError(f"{tag}: ad_tag must be a hex string")
            try:
                raw = bytes.fromhex(ad_tag)
            except ValueError:
                raise ValueError(f"{tag}: ad_tag must be a valid hex string") from None
            if not (1 <= len(raw) <= 255):
                raise ValueError(f"{tag}: ad_tag must decode to 1-255 bytes")

    def _resolve_instances(self):
        for instance in self["instances"]:
            self._read_instance(instance)
        self._protocols = _protocols_from_inbounds_by_tag(self._inbounds_by_tag)

    def _read_instance(self, instance: dict):
        tag = instance["tag"]
        if tag in self.exclude_inbound_tags:
            return

        metadata = {
            "tag": tag,
            "protocol": "mtproto",
            "network": "tcp",
            "tls": "tls",
            "port": instance.get("port"),
            "sni": instance.get("fake_tls_domain", ""),
            # Same generic per-inbound extra-data channel OpenVPN/Hysteria2
            # use (app.core.hosts) - carries the fake-TLS domain through to
            # _build_mtproto_components (app/subscription/base.py), which
            # can't get it from a bare host/port alone.
            "finalmask": {
                "mtproto": {
                    "fake_tls_domain": instance.get("fake_tls_domain", ""),
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
    def from_json(cls, data: dict) -> "MTProtoConfig":
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
