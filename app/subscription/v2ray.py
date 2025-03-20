import base64
import json
import urllib.parse as urlparse
from random import choice
from typing import Union
from urllib.parse import quote
from uuid import UUID
from . import BaseSubscription
from app.subscription.funcs import get_grpc_gun, get_grpc_multi
from config import EXTERNAL_CONFIG


class V2rayShareLink(BaseSubscription):
    def __init__(self):
        super().__init__()
        self.links = []

    def add_link(self, link):
        self.links.append(link)

    def render(self, reverse=False):
        if EXTERNAL_CONFIG:
            self.links.append(EXTERNAL_CONFIG)
        if reverse:
            self.links.reverse()
        return self.links

    def add(self, remark: str, address: str, inbound: dict, settings: dict):
        net = inbound["network"]
        multi_mode = inbound.get("multi_mode", False)
        old_path: str = inbound["path"]

        if net in ("grpc", "gun"):
            if multi_mode:
                path = get_grpc_multi(old_path)
            else:
                path = get_grpc_gun(old_path)
            if old_path.startswith("/"):
                path = quote(path, safe="-_.!~*'()")

        else:
            path = old_path
        func_args = dict(
            remark=remark,
            address=address,
            port=inbound["port"],
            net=net,
            tls=inbound["tls"],
            sni=inbound.get("sni", ""),
            fp=inbound.get("fp", ""),
            alpn=inbound.get("alpn", ""),
            pbk=inbound.get("pbk", ""),
            sid=inbound.get("sid", ""),
            spx=inbound.get("spx", ""),
            host=inbound["host"],
            path=path,
            type=inbound["header_type"],
            ais=inbound.get("ais", ""),
            fs=inbound.get("fragment_settings", ""),
            multiMode=multi_mode,
            sc_max_each_post_bytes=inbound.get("sc_max_each_post_bytes"),
            sc_max_concurrent_posts=inbound.get("sc_max_concurrent_posts"),
            sc_min_posts_interval_ms=inbound.get("sc_min_posts_interval_ms"),
            x_padding_bytes=inbound.get("x_padding_bytes"),
            mode=inbound.get("mode", ""),
            noGRPCHeader=inbound.get("no_grpc_header"),
            heartbeatPeriod=inbound.get("heartbeat_period", 0),
            scStreamUpServerSecs=inbound.get("sc_stream_up_server_secs"),
            xmux=inbound.get("xmux"),
            downloadSettings=inbound.get("downloadSettings"),
            http_headers=inbound.get("http_headers"),
        )
        if inbound["protocol"] == "vmess":
            link = self.vmess(
                id=settings["id"],
                **func_args,
            )

        elif inbound["protocol"] == "vless":
            link = self.vless(
                id=settings["id"],
                flow=settings.get("flow", ""),
                **func_args,
            )

        elif inbound["protocol"] == "trojan":
            link = self.trojan(
                password=settings["password"],
                flow=settings.get("flow", ""),
                **func_args,
            )

        elif inbound["protocol"] == "shadowsocks":
            link = self.shadowsocks(
                remark=remark,
                address=address,
                port=inbound["port"],
                password=settings["password"],
                method=settings["method"],
            )
        else:
            return

        self.add_link(link=link)

    def vmess(
        self,
        remark: str,
        address: str,
        port: int,
        id: Union[str, UUID],
        host="",
        net="tcp",
        path="",
        type="",
        tls="none",
        sni="",
        fp="",
        alpn="",
        pbk="",
        sid="",
        spx="",
        ais="",
        fs="",
        multiMode: bool = False,
        sc_max_each_post_bytes: int | None = None,
        sc_max_concurrent_posts: int | None = None,
        sc_min_posts_interval_ms: int | None = None,
        x_padding_bytes: str | None = None,
        mode: str = "",
        noGRPCHeader: bool | None = None,
        heartbeatPeriod: int | None = None,
        scStreamUpServerSecs: int | None = None,
        xmux: dict | None = None,
        downloadSettings: dict | None = None,
        random_user_agent: bool = False,
        http_headers: dict | None = None,
    ):
        payload = {
            "add": address,
            "aid": "0",
            "host": host,
            "id": str(id),
            "net": net,
            "path": path,
            "port": port,
            "ps": remark,
            "scy": "auto",
            "tls": tls,
            "type": type,
            "v": "2",
        }

        if fs:
            payload["fragment"] = fs

        if tls == "tls":
            payload["sni"] = sni
            payload["fp"] = fp
            if alpn:
                payload["alpn"] = alpn
            if fs:
                payload["fragment"] = fs
            if ais:
                payload["allowInsecure"] = 1

        elif tls == "reality":
            payload["sni"] = sni
            payload["fp"] = fp
            payload["pbk"] = pbk
            payload["sid"] = sid
            if spx:
                payload["spx"] = spx

        if net == "grpc":
            if multiMode:
                payload["mode"] = "multi"
            else:
                payload["mode"] = "gun"

        elif net in ("splithttp", "xhttp"):
            extra = {
                "scMaxEachPostBytes": sc_max_each_post_bytes,
                "scMaxConcurrentPosts": sc_max_concurrent_posts,
                "scMinPostsIntervalMs": sc_min_posts_interval_ms,
                "xPaddingBytes": x_padding_bytes,
                "noGRPCHeader": noGRPCHeader,
                "scStreamUpServerSecs": scStreamUpServerSecs,
                "xmux": xmux,
                "downloadSettings": downloadSettings,
                "headers": http_headers if http_headers is not None else {},
            }
            if random_user_agent:
                if mode in ("stream-one", "stream-up") and not noGRPCHeader:
                    extra["headers"]["User-Agent"] = choice(self.grpc_user_agent_data)
                else:
                    extra["headers"]["User-Agent"] = choice(self.user_agent_list)

            extra = self._remove_none_values(extra)

            payload["type"] = mode
            if extra:
                payload["extra"] = (json.dumps(extra)).replace(" ", "")

        elif net == "ws":
            if heartbeatPeriod:
                payload["heartbeatPeriod"] = heartbeatPeriod

        return "vmess://" + base64.b64encode(json.dumps(payload, sort_keys=True).encode("utf-8")).decode()

    def vless(
        self,
        remark: str,
        address: str,
        port: int,
        id: Union[str, UUID],
        net="ws",
        path="",
        host="",
        type="",
        flow="",
        tls="none",
        sni="",
        fp="",
        alpn="",
        pbk="",
        sid="",
        spx="",
        ais="",
        fs="",
        multiMode: bool = False,
        sc_max_each_post_bytes: int | None = None,
        sc_max_concurrent_posts: int | None = None,
        sc_min_posts_interval_ms: int | None = None,
        x_padding_bytes: str | None = None,
        mode: str = "",
        noGRPCHeader: bool | None = None,
        heartbeatPeriod: int | None = None,
        scStreamUpServerSecs: int | None = None,
        http_headers: dict | None = None,
        xmux: dict | None = None,
        random_user_agent: bool = False,
        downloadSettings: dict | None = None,
    ):
        payload = {"security": tls, "type": net, "headerType": type}
        if flow and (tls in ("tls", "reality") and net in ("tcp", "raw", "kcp") and type != "http"):
            payload["flow"] = flow

        if net == "grpc":
            payload["serviceName"] = path
            payload["authority"] = host
            if multiMode:
                payload["mode"] = "multi"
            else:
                payload["mode"] = "gun"

        elif net == "quic":
            payload["key"] = path
            payload["quicSecurity"] = host

        elif net in ("splithttp", "xhttp"):
            payload["path"] = path
            payload["host"] = host
            payload["mode"] = mode
            extra = {
                "scMaxEachPostBytes": sc_max_each_post_bytes,
                "scMaxConcurrentPosts": sc_max_concurrent_posts,
                "scMinPostsIntervalMs": sc_min_posts_interval_ms,
                "xPaddingBytes": x_padding_bytes,
                "noGRPCHeader": noGRPCHeader,
                "scStreamUpServerSecs": scStreamUpServerSecs,
                "xmux": xmux,
                "downloadSettings": downloadSettings,
                "headers": http_headers if http_headers is not None else {},
            }
            if random_user_agent:
                if mode in ("stream-one", "stream-up") and not noGRPCHeader:
                    extra["headers"]["User-Agent"] = choice(self.grpc_user_agent_data)
                else:
                    extra["headers"]["User-Agent"] = choice(self.user_agent_list)

            extra = self._remove_none_values(extra)

            if extra:
                payload["extra"] = (json.dumps(extra)).replace(" ", "")

        elif net == "kcp":
            payload["seed"] = path
            payload["host"] = host

        elif net == "ws":
            payload["path"] = path
            payload["host"] = host
            if heartbeatPeriod:
                payload["heartbeatPeriod"] = heartbeatPeriod

        else:
            payload["path"] = path
            payload["host"] = host

        if tls == "tls":
            payload["sni"] = sni
            payload["fp"] = fp
            if alpn:
                payload["alpn"] = alpn
            if fs:
                payload["fragment"] = fs
            if ais:
                payload["allowInsecure"] = 1

        elif tls == "reality":
            payload["sni"] = sni
            payload["fp"] = fp
            payload["pbk"] = pbk
            payload["sid"] = sid
            if spx:
                payload["spx"] = spx

        return "vless://" + f"{id}@{address}:{port}?" + urlparse.urlencode(payload) + f"#{(urlparse.quote(remark))}"

    def trojan(
        self,
        remark: str,
        address: str,
        port: int,
        password: str,
        net="tcp",
        path="",
        host="",
        type="",
        flow="",
        tls="none",
        sni="",
        fp="",
        alpn="",
        pbk="",
        sid="",
        spx="",
        ais="",
        fs="",
        multiMode: bool = False,
        sc_max_each_post_bytes: int | None = None,
        sc_max_concurrent_posts: int | None = None,
        sc_min_posts_interval_ms: int | None = None,
        x_padding_bytes: str | None = None,
        mode: str = "",
        noGRPCHeader: bool | None = None,
        heartbeatPeriod: int | None = None,
        scStreamUpServerSecs: int | None = None,
        http_headers: dict | None = None,
        xmux: dict | None = None,
        random_user_agent: bool = False,
        downloadSettings: dict | None = None,
    ):
        payload = {"security": tls, "type": net, "headerType": type}
        if flow and (tls in ("tls", "reality") and net in ("tcp", "raw", "kcp") and type != "http"):
            payload["flow"] = flow

        if net == "grpc":
            payload["serviceName"] = path
            payload["authority"] = host
            if multiMode:
                payload["mode"] = "multi"
            else:
                payload["mode"] = "gun"

        elif net in ("splithttp", "xhttp"):
            payload["path"] = path
            payload["host"] = host
            payload["mode"] = mode
            extra = {
                "scMaxEachPostBytes": sc_max_each_post_bytes,
                "scMaxConcurrentPosts": sc_max_concurrent_posts,
                "scMinPostsIntervalMs": sc_min_posts_interval_ms,
                "xPaddingBytes": x_padding_bytes,
                "noGRPCHeader": noGRPCHeader,
                "scStreamUpServerSecs": scStreamUpServerSecs,
                "xmux": xmux,
                "downloadSettings": downloadSettings,
                "headers": http_headers if http_headers is not None else {},
            }
            if random_user_agent:
                if mode in ("stream-one", "stream-up") and not noGRPCHeader:
                    extra["headers"]["User-Agent"] = choice(self.grpc_user_agent_data)
                else:
                    extra["headers"]["User-Agent"] = choice(self.user_agent_list)

            extra = self._remove_none_values(extra)

            if extra:
                payload["extra"] = (json.dumps(extra)).replace(" ", "")

        elif net == "quic":
            payload["key"] = path
            payload["quicSecurity"] = host

        elif net == "kcp":
            payload["seed"] = path
            payload["host"] = host

        elif net == "ws":
            payload["path"] = path
            payload["host"] = host
            if heartbeatPeriod:
                payload["heartbeatPeriod"] = heartbeatPeriod

        else:
            payload["path"] = path
            payload["host"] = host

        if tls == "tls":
            payload["sni"] = sni
            payload["fp"] = fp
            if alpn:
                payload["alpn"] = alpn
            if fs:
                payload["fragment"] = fs
            if ais:
                payload["allowInsecure"] = 1
        elif tls == "reality":
            payload["sni"] = sni
            payload["fp"] = fp
            payload["pbk"] = pbk
            payload["sid"] = sid
            if spx:
                payload["spx"] = spx

        return (
            "trojan://"
            + f"{urlparse.quote(password, safe=':')}@{address}:{port}?"
            + urlparse.urlencode(payload)
            + f"#{urlparse.quote(remark)}"
        )

    def shadowsocks(self, remark: str, address: str, port: int, password: str, method: str):
        return (
            "ss://"
            + base64.b64encode(f"{method}:{password}".encode()).decode()
            + f"@{address}:{port}#{urlparse.quote(remark)}"
        )
