from __future__ import annotations

import base64
import json
from copy import deepcopy
from pathlib import PosixPath

import commentjson

from app.models.core import CoreType
from app.models.protocol import ProxyProtocol
from app.utils.crypto import get_x25519_public_key

_SING_BOX_INBOUND_PROTOCOLS = ("vless", "vmess", "trojan", "shadowsocks", "hysteria2")


def _protocols_from_inbounds_by_tag(inbounds_by_tag: dict[str, dict]) -> frozenset[ProxyProtocol]:
    return frozenset(
        protocol
        for inbound in inbounds_by_tag.values()
        if (protocol := ProxyProtocol.from_value(inbound["protocol"])) is not None
    )


class SingBoxConfig(dict):
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

        self._transport_handlers = {
            "ws": self._handle_ws_transport,
            "grpc": self._handle_grpc_transport,
            "http": self._handle_http_transport,
            "httpupgrade": self._handle_httpupgrade_transport,
            "quic": self._handle_quic_transport,
        }

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

            itype = inbound.get("type")
            if itype == "hysteria2":
                self._validate_hysteria2_inbound(inbound)
            elif itype == "shadowsocks":
                self._validate_shadowsocks_inbound(inbound)

    def _validate_hysteria2_inbound(self, inbound: dict):
        tag = inbound["tag"]
        tls = inbound.get("tls") or {}
        if not tls.get("enabled"):
            raise ValueError(f"{tag}: hysteria2 inbound requires tls to be enabled")

    def _validate_shadowsocks_inbound(self, inbound: dict):
        if not inbound.get("method"):
            raise ValueError(f"{inbound['tag']}: shadowsocks inbound requires a method")

    def _resolve_inbounds(self):
        for inbound in self["inbounds"]:
            self._read_inbound(inbound)
        self._protocols = _protocols_from_inbounds_by_tag(self._inbounds_by_tag)

    def _read_inbound(self, inbound: dict):
        itype = inbound.get("type")
        if itype not in _SING_BOX_INBOUND_PROTOCOLS:
            return

        tag = inbound["tag"]
        if tag in self.exclude_inbound_tags:
            return

        if itype == "hysteria2":
            metadata = self._read_hysteria2_inbound(inbound)
        else:
            metadata = self._read_generic_inbound(inbound)

        self._inbounds.append(tag)
        self._inbounds_by_tag[tag] = metadata

    def _read_hysteria2_inbound(self, inbound: dict) -> dict:
        tag = inbound["tag"]
        tls = inbound.get("tls") or {}
        obfs = inbound.get("obfs")
        finalmask = None
        if isinstance(obfs, dict) and obfs.get("type") == "salamander" and obfs.get("password"):
            finalmask = {"udp": [{"type": "salamander", "settings": {"password": obfs["password"]}}]}

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
        return {
            "tag": tag,
            "protocol": inbound["type"],
            "network": "udp",
            "tls": "tls",
            "port": inbound.get("listen_port"),
            "sni": tls.get("server_name", ""),
            "finalmask": finalmask,
        }

    def _read_generic_inbound(self, inbound: dict) -> dict:
        tag = inbound["tag"]
        itype = inbound["type"]

        inbound["users"] = []

        settings = {
            "tag": tag,
            "protocol": itype,
            "port": inbound.get("listen_port"),
            "network": "tcp",
            "tls": "none",
            "sni": [],
            "host": [],
            "path": "",
            "header_type": "",
            "finalmask": None,
        }

        if itype == "vless":
            settings["flow"] = ""
            settings["encryption"] = "none"
        elif itype == "shadowsocks":
            self._handle_shadowsocks_settings(inbound, settings)

        transport = inbound.get("transport")
        if isinstance(transport, dict):
            handler = self._transport_handlers.get(transport.get("type"))
            if handler:
                handler(transport, settings)

        tls = inbound.get("tls")
        if isinstance(tls, dict) and tls.get("enabled"):
            self._handle_tls_settings(tls, settings, tag)

        return settings

    def _handle_shadowsocks_settings(self, inbound: dict, settings: dict):
        method = inbound.get("method", "")
        settings["method"] = method
        if method == "2022-blake3-chacha20-poly1305":
            raise ValueError("only 2022-blake3-aes-*-gcm methods are supported")
        if method.startswith("2022-blake3"):
            settings["is_2022"] = True
            password = inbound.get("password", "")
            try:
                base64.b64decode(password, validate=True)
                settings["password"] = password
            except Exception:
                raise ValueError("Shadowsocks password must be a valid base64 string")
        else:
            settings["is_2022"] = False
        settings["header_type"] = "none"

    def _handle_ws_transport(self, transport: dict, settings: dict):
        settings["network"] = "ws"
        settings["path"] = transport.get("path", "") or ""
        headers = transport.get("headers") or {}
        host = headers.get("Host") or headers.get("host") or ""
        if host:
            settings["host"] = [host] if isinstance(host, str) else list(host)
        settings["header_type"] = ""

    def _handle_grpc_transport(self, transport: dict, settings: dict):
        settings["network"] = "grpc"
        settings["path"] = transport.get("service_name", "") or ""
        settings["header_type"] = ""

    def _handle_http_transport(self, transport: dict, settings: dict):
        settings["network"] = "http"
        settings["path"] = transport.get("path", "") or ""
        host = transport.get("host", [])
        if isinstance(host, str):
            host = [host]
        settings["host"] = list(host) if host else []
        settings["header_type"] = ""

    def _handle_httpupgrade_transport(self, transport: dict, settings: dict):
        settings["network"] = "httpupgrade"
        settings["path"] = transport.get("path", "") or ""
        host = transport.get("host", "")
        if host:
            settings["host"] = [host] if isinstance(host, str) else list(host)
        settings["header_type"] = ""

    def _handle_quic_transport(self, transport: dict, settings: dict):
        settings["network"] = "quic"
        settings["header_type"] = ""

    def _handle_tls_settings(self, tls: dict, settings: dict, inbound_tag: str):
        reality = tls.get("reality")
        is_reality = isinstance(reality, dict) and reality.get("enabled")
        settings["tls"] = "reality" if is_reality else "tls"

        sni = tls.get("server_name")
        if not sni and is_reality:
            sni = (reality.get("handshake") or {}).get("server")
        if sni:
            settings["sni"] = [sni]

        alpn = tls.get("alpn")
        if isinstance(alpn, list) and alpn:
            settings["alpn"] = list(alpn)

        utls = tls.get("utls")
        if isinstance(utls, dict) and utls.get("fingerprint"):
            settings["fp"] = utls["fingerprint"]

        if "insecure" in tls:
            settings["allowinsecure"] = bool(tls.get("insecure"))

        if is_reality:
            self._handle_reality_settings(reality, settings, inbound_tag)

    def _handle_reality_settings(self, reality: dict, settings: dict, inbound_tag: str):
        private_key = reality.get("private_key")
        if not private_key:
            raise ValueError(f"{inbound_tag}: reality inbound requires tls.reality.private_key")
        try:
            settings["pbk"] = get_x25519_public_key(private_key)
        except Exception as exc:
            raise ValueError(f"{inbound_tag}: invalid reality private_key") from exc
        settings["sids"] = list(reality.get("short_id") or [])
        settings["spx"] = ""

    _PANEL_ONLY_INBOUND_KEYS = ("port_hopping_range", "up_mbps", "down_mbps")

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
