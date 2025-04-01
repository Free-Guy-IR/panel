from __future__ import annotations

import json
from copy import deepcopy
from pathlib import PosixPath
from typing import Union

import commentjson

from app.models.proxy import ProxyTypes
from app.utils.crypto import get_cert_SANs, get_x25519_public_key
from config import XRAY_EXCLUDE_INBOUND_TAGS, XRAY_FALLBACKS_INBOUND_TAGS


class XRayConfig(dict):
    def __init__(self, config: Union[dict, str, PosixPath] = {}):
        """Initialize the XRay config."""
        if isinstance(config, str):
            try:
                # considering string as json
                config = commentjson.loads(config)
            except (json.JSONDecodeError, ValueError):
                # considering string as file path
                with open(config, "r") as file:
                    config = commentjson.loads(file.read())

        if isinstance(config, PosixPath):
            with open(config, "r") as file:
                config = commentjson.loads(file.read())

        if isinstance(config, dict):
            config = deepcopy(config)

        super().__init__(config)
        self._validate()

        self.inbounds = []
        self.inbounds_by_tag = {}
        self._fallbacks_inbound = []
        for tag in XRAY_FALLBACKS_INBOUND_TAGS:
            self._fallbacks_inbound.append(self.get_inbound(tag))
        self._resolve_inbounds()

    def _validate(self):
        """Validate the config."""
        if not self.get("inbounds"):
            raise ValueError("config doesn't have inbounds")

        if not self.get("outbounds"):
            raise ValueError("config doesn't have outbounds")

        for inbound in self["inbounds"]:
            if not inbound.get("tag"):
                raise ValueError("all inbounds must have a unique tag")
            if "," in inbound.get("tag"):
                raise ValueError("character «,» is not allowed in inbound tag")
            if "<=>" in inbound.get("tag"):
                raise ValueError("character «<=>» is not allowed in inbound tag")
        for outbound in self["outbounds"]:
            if not outbound.get("tag"):
                raise ValueError("all outbounds must have a unique tag")

    def _find_fallback_inbound(self, inbound: dict) -> list:
        """Find fallback inbounds for an inbound."""
        fallback_inbounds = []
        for fallback in self._fallbacks_inbound:
            for fallback_settings in fallback.get("settings", {}).get("fallbacks", []):
                try:
                    if fallback_settings["dest"] == inbound["listen"]:
                        fallback_inbounds.append(fallback)
                except KeyError:
                    continue
        return fallback_inbounds

    def _create_base_settings(self, inbound: dict) -> dict:
        """Create base settings for an inbound."""
        settings = {
            "tag": inbound["tag"],
            "protocol": inbound["protocol"],
            "port": None,
            "network": "tcp",
            "tls": "none",
            "sni": [],
            "host": [],
            "path": "",
            "header_type": "",
            "is_fallback": False,
            "fallbacks": [],
        }
        return settings

    def _handle_port_settings(self, inbound: dict, settings: dict):
        """Handle port settings for an inbound."""
        try:
            settings["port"] = inbound["port"]
        except KeyError:
            if self._fallbacks_inbound:
                fallbacks = self._find_fallback_inbound(inbound)
                if fallbacks:
                    settings["is_fallback"] = True
                    settings["fallbacks"] = fallbacks

    def _handle_tls_settings(self, tls_settings: dict, settings: dict, inbound_tag: str):
        """Handle TLS security settings."""
        settings["tls"] = "tls"
        for certificate in tls_settings.get("certificates", []):
            if certificate.get("certificateFile", None):
                with open(certificate["certificateFile"], "rb") as file:
                    cert = file.read()
                    settings["sni"].extend(get_cert_SANs(cert))

            if certificate.get("certificate", None):
                cert = certificate["certificate"]
                if isinstance(cert, list):
                    cert = "\n".join(cert)
                if isinstance(cert, str):
                    cert = cert.encode()
                settings["sni"].extend(get_cert_SANs(cert))

    def _handle_reality_settings(self, tls_settings: dict, settings: dict, inbound_tag: str):
        """Handle Reality security settings."""
        settings["fp"] = "chrome"
        settings["tls"] = "reality"
        settings["sni"] = tls_settings.get("serverNames", [])

        pvk = tls_settings.get("privateKey")
        if not pvk:
            raise ValueError(f"You need to provide privateKey in realitySettings of {inbound_tag}")

        settings["pbk"] = get_x25519_public_key(pvk)
        if not settings.get("pbk"):
            raise ValueError(f"You need to provide publicKey in realitySettings of {inbound_tag}")

        try:
            settings["sids"] = tls_settings.get("shortIds")
            settings["sids"][0]  # check if there is any shortIds
        except (IndexError, TypeError):
            raise ValueError(f"You need to define at least one shortID in realitySettings of {inbound_tag}")
        try:
            settings["spx"] = tls_settings.get("SpiderX")
        except Exception:
            settings["spx"] = ""

    def _handle_network_settings(self, net: str, net_settings: dict, settings: dict, inbound_tag: str):
        """Handle network-specific settings."""
        if net in ("tcp", "raw"):
            self._handle_tcp_raw_settings(net_settings, settings, inbound_tag)
        elif net == "ws":
            self._handle_ws_settings(net_settings, settings)
        elif net in ("grpc", "gun"):
            self._handle_grpc_settings(net_settings, settings)
        elif net == "quic":
            self._handle_quic_settings(net_settings, settings)
        elif net == "httpupgrade":
            self._handle_httpupgrade_settings(net_settings, settings)
        elif net in ("splithttp", "xhttp"):
            self._handle_xhttp_settings(net_settings, settings)
        elif net == "kcp":
            self._handle_kcp_settings(net_settings, settings)
        elif net in ("http", "h2", "h3"):
            self._handle_http_settings(net_settings, settings)
        else:
            self._handle_default_network_settings(net_settings, settings)

    def _handle_tcp_raw_settings(self, net_settings: dict, settings: dict, inbound_tag: str):
        """Handle TCP and RAW network settings."""
        header = net_settings.get("header", {})
        request = header.get("request", {})
        path = request.get("path")
        host = request.get("headers", {}).get("Host")

        settings["header_type"] = header.get("type", "")

        if isinstance(path, str) or isinstance(host, str):
            raise ValueError(
                f"Settings of {inbound_tag} for path and host must be list, not str\n"
                "https://xtls.github.io/config/transports/tcp.html#httpheaderobject"
            )

        if path and isinstance(path, list):
            settings["path"] = path[0]

        if host and isinstance(host, list):
            settings["host"] = host

    def _handle_ws_settings(self, net_settings: dict, settings: dict):
        """Handle WebSocket network settings."""
        path = net_settings.get("path", "")
        host = net_settings.get("host", "") or net_settings.get("headers", {}).get("Host")

        settings["header_type"] = ""

        if isinstance(path, list) or isinstance(host, list):
            raise ValueError(
                "Settings for path and host must be str, not list\n"
                "https://xtls.github.io/config/transports/websocket.html#websocketobject"
            )

        if isinstance(path, str):
            settings["path"] = path

        if isinstance(host, str):
            settings["host"] = [host]

    def _handle_grpc_settings(self, net_settings: dict, settings: dict):
        """Handle gRPC network settings."""
        settings["header_type"] = ""
        settings["path"] = net_settings.get("serviceName", "")
        host = net_settings.get("authority", "")
        settings["host"] = [host]

    def _handle_quic_settings(self, net_settings: dict, settings: dict):
        """Handle QUIC network settings."""
        settings["header_type"] = net_settings.get("header", {}).get("type", "")
        settings["path"] = net_settings.get("key", "")
        settings["host"] = [net_settings.get("security", "")]

    def _handle_httpupgrade_settings(self, net_settings: dict, settings: dict):
        """Handle HTTP Upgrade network settings."""
        settings["path"] = net_settings.get("path", "")
        host = net_settings.get("host", "")
        settings["host"] = [host]

    def _handle_xhttp_settings(self, net_settings: dict, settings: dict):
        """Handle XHTTP network settings."""
        settings["path"] = net_settings.get("path", "")
        host = net_settings.get("host", "")
        settings["host"] = [host]
        settings["mode"] = net_settings.get("mode", "auto")

    def _handle_kcp_settings(self, net_settings: dict, settings: dict):
        """Handle KCP network settings."""
        header = net_settings.get("header", {})
        settings["header_type"] = header.get("type", "")
        settings["host"] = header.get("domain", "")
        settings["path"] = net_settings.get("seed", "")

    def _handle_http_settings(self, net_settings: dict, settings: dict):
        """Handle HTTP network settings."""
        settings["host"] = net_settings.get("host") or net_settings.get("Host", "")
        settings["path"] = net_settings.get("path", "")

    def _handle_default_network_settings(self, net_settings: dict, settings: dict):
        """Handle default network settings."""
        settings["path"] = net_settings.get("path", "")
        host = net_settings.get("host", {}) or net_settings.get("Host", {})
        if host and isinstance(host, str):
            settings["host"] = host
        elif host and isinstance(host, list):
            settings["host"] = host[0]

    def _resolve_inbounds(self):
        """Resolve all inbounds and their settings."""
        for inbound in self["inbounds"]:
            self._read_inbound(inbound)

    def _read_inbound(self, inbound: dict):
        """Read an inbound and its settings."""
        if inbound["protocol"] not in ProxyTypes._value2member_map_:
            return

        if inbound["tag"] in XRAY_EXCLUDE_INBOUND_TAGS:
            return

        if not inbound.get("settings"):
            inbound["settings"] = {}
        if not inbound["settings"].get("clients"):
            inbound["settings"]["clients"] = []

        settings = self._create_base_settings(inbound)
        self._handle_port_settings(inbound, settings)

        if stream := inbound.get("streamSettings"):
            net = stream.get("network", "tcp")
            net_settings = stream.get(f"{net}Settings", {})
            security = stream.get("security")
            tls_settings = stream.get(f"{security}Settings")

            if settings["is_fallback"] is True:
                for fallback in settings["fallbacks"]:
                    fallback_tag = f"{inbound['tag']}<=>{fallback['tag']}"  # a fake inbound tag
                    if fallback_tag in self.inbounds_by_tag:
                        continue
                    try:
                        fallback_port = fallback["port"]
                    except KeyError:
                        raise ValueError("fallbacks inbound doesn't have port")
                    fallback_security = fallback.get("streamSettings", {}).get("security")
                    fallback_tls_settings = fallback.get("streamSettings", {}).get(f"{fallback_security}Settings", {})
                    fallback_inbound = self._make_fallback_inbound(
                        inbound, fallback_security, fallback_tls_settings, fallback_tag, fallback_port
                    )
                    self._read_inbound(fallback_inbound)

            settings["network"] = net

            if security == "tls":
                self._handle_tls_settings(tls_settings, settings, inbound["tag"])
            elif security == "reality":
                self._handle_reality_settings(tls_settings, settings, inbound["tag"])

            self._handle_network_settings(net, net_settings, settings, inbound["tag"])

        if inbound["tag"] not in self.inbounds:
            self.inbounds.append(inbound["tag"])
            self.inbounds_by_tag[inbound["tag"]] = settings

    def _make_fallback_inbound(
        self,
        inbound: dict,
        security: str,
        tls_settings: dict,
        inbound_tag: str,
        port: int | str,
    ):
        """Make a fallback inbound."""
        fallback_inbound = {
            **inbound,
            "port": port,
            "tag": inbound_tag,
        }
        fallback_inbound["streamSettings"]["security"] = security
        fallback_inbound["streamSettings"][f"{security}Settings"] = tls_settings
        return fallback_inbound

    def get_inbound(self, tag) -> dict:
        """Get an inbound by tag."""
        for inbound in self["inbounds"]:
            if inbound["tag"] == tag:
                return inbound

    def get_outbound(self, tag) -> dict:
        """Get an outbound by tag."""
        for outbound in self["outbounds"]:
            if outbound["tag"] == tag:
                return outbound

    def to_json(self, **json_kwargs):
        """Convert the config to a JSON string."""
        return json.dumps(self, **json_kwargs)

    def copy(self):
        """Copy the config."""
        return deepcopy(self)
