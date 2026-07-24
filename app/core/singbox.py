from __future__ import annotations

import json
from copy import deepcopy
from pathlib import PosixPath
from typing import Union

import commentjson

from app.models.core import CoreType
from app.models.protocol import ProxyProtocol

_SING_BOX_INBOUND_PROTOCOLS = ("hysteria2",)


def _protocols_from_inbounds_by_tag(inbounds_by_tag: dict[str, dict]) -> frozenset[ProxyProtocol]:
    return frozenset(
        protocol
        for inbound in inbounds_by_tag.values()
        if (protocol := ProxyProtocol.from_value(inbound["protocol"])) is not None
    )


class SingBoxConfig(dict):
    """AbstractCore implementation for sing-box.

    Scope (v1): only the ``hysteria2`` inbound type is recognized. sing-box
    supports many more protocols, but per-protocol support here is added
    incrementally as needed - unlike XRayConfig's network_handlers registry,
    there is intentionally no generic multi-protocol resolver yet.
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

        self._type = CoreType.singbox
        self.exclude_inbound_tags = set(exclude_inbound_tags or set())
        self.fallbacks_inbound_tags = set(fallbacks_inbound_tags or set())
        self._inbounds: list[str] = []
        self._inbounds_by_tag: dict[str, dict] = {}
        self._protocols: frozenset[ProxyProtocol] = frozenset()

        if skip_validation:
            return

        self._validate()
        self._resolve_inbounds()

    @property
    def type(self) -> str:
        return self._type

    def _validate(self):
        if self.fallbacks_inbound_tags:
            raise ValueError("fallbacks_inbound_tags is not supported for sing-box cores")

        if not self.get("inbounds"):
            raise ValueError("config doesn't have inbounds")
        if not isinstance(self["inbounds"], list):
            raise ValueError("inbounds must be a list")

        if not self.get("outbounds"):
            raise ValueError("config doesn't have outbounds")

        seen_tags: set[str] = set()
        for inbound in self["inbounds"]:
            tag = inbound.get("tag")
            if not tag:
                raise ValueError("all inbounds must have a unique tag")
            if tag in seen_tags:
                raise ValueError(f"duplicate inbound tag: {tag}")
            seen_tags.add(tag)

            if inbound.get("type") == "hysteria2":
                self._validate_hysteria2_inbound(inbound)

    def _validate_hysteria2_inbound(self, inbound: dict):
        tag = inbound["tag"]
        tls = inbound.get("tls") or {}
        if not tls.get("enabled"):
            raise ValueError(f"{tag}: hysteria2 inbound requires tls to be enabled")

    def _resolve_inbounds(self):
        for inbound in self["inbounds"]:
            self._read_inbound(inbound)
        self._protocols = _protocols_from_inbounds_by_tag(self._inbounds_by_tag)

    def _read_inbound(self, inbound: dict):
        if inbound.get("type") not in _SING_BOX_INBOUND_PROTOCOLS:
            return

        tag = inbound["tag"]
        if tag in self.exclude_inbound_tags:
            return

        tls = inbound.get("tls") or {}
        metadata = {
            "tag": tag,
            "protocol": inbound["type"],
            "network": "udp",
            "tls": "tls",
            "port": inbound.get("listen_port"),
            "sni": tls.get("server_name", ""),
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
    def from_json(cls, data: dict) -> "SingBoxConfig":
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
