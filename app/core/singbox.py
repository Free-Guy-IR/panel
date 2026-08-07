from __future__ import annotations

import json
from copy import deepcopy
from pathlib import PosixPath

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
            raise TypeError("inbounds must be a list")

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
        obfs = inbound.get("obfs")
        finalmask = None
        if isinstance(obfs, dict) and obfs.get("type") == "salamander" and obfs.get("password"):
            # Shaped to match app.core.hosts._prepare_subscription_inbound_datas
            # inbound_config.get("finalmask") fallback, and _get_hysteria_data_from_finalmasks
            # expected input - this makes sing-box Salamander obfuscation flow through the
            # exact same subscription-link pipeline xrays own hysteria obfs already uses,
            # with no changes needed to hosts.py itself.
            finalmask = {"udp": [{"type": "salamander", "settings": {"password": obfs["password"]}}]}

        # sing-box's hysteria2 inbound has no native port-hopping concept - the range below
        # is informational only, advertised to clients via mports so hopping-aware clients
        # can use it, and must be kept in sync with whatever DNAT/redirect rule (if any)
        # actually forwards that range to this inbounds real listen_port on the node host.
        up_mbps = inbound.get("up_mbps")
        down_mbps = inbound.get("down_mbps")
        hop_ports = inbound.get("port_hopping_range")
        if up_mbps or down_mbps or hop_ports:
            quic_params = (finalmask or {}).get("quicParams", {})
            if up_mbps:
                quic_params["brutalUp"] = f"{up_mbps} mbps"
            if down_mbps:
                quic_params["brutalDown"] = f"{down_mbps} mbps"
            if hop_ports:
                quic_params["udpHop"] = {"ports": hop_ports}
            finalmask = finalmask or {}
            finalmask["quicParams"] = quic_params
        metadata = {
            "tag": tag,
            "protocol": inbound["type"],
            "network": "udp",
            "tls": "tls",
            "port": inbound.get("listen_port"),
            "sni": tls.get("server_name", ""),
            "finalmask": finalmask,
        }

        self._inbounds.append(tag)
        self._inbounds_by_tag[tag] = metadata

    # Keys that exist only for the panels own subscription-link metadata
    # (finalmask/quicParams sourcing) and are not part of sing-boxs actual
    # schema - sing-box uses strict JSON decoding and errors out on any
    # unrecognized inbound field, so these must never reach the wire config.
    _PANEL_ONLY_INBOUND_KEYS = ("port_hopping_range",)

    def to_str(self, **json_kwargs) -> str:
        wire_config = deepcopy(dict(self))
        for inbound in wire_config.get("inbounds", []):
            for key in self._PANEL_ONLY_INBOUND_KEYS:
                inbound.pop(key, None)
        return json.dumps(wire_config, **json_kwargs)

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
    def from_json(cls, data: dict) -> SingBoxConfig:
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
