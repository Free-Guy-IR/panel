import asyncio
import base64
import json
import re
import time
from typing import ClassVar
from urllib.parse import quote, urlsplit, urlunsplit

from app.db import AsyncSession
from app.models.settings import ConfigFormat
from app.models.user import UsersResponseWithInbounds

EXTRA_CLIENT_CONFIG = {
    ConfigFormat.openvpn: {
        "config_format": "openvpn",
        "media_type": "application/zip",
        "as_base64": False,
        "extension": ".zip",
    },
    ConfigFormat.l2tp: {
        "config_format": "l2tp",
        "media_type": "application/json",
        "as_base64": False,
        "extension": ".json",
    },
}


class SubscriptionExtrasMixin:
    _PING_SKIP_SCHEMES: ClassVar[set[str]] = {"wireguard", "wg", "openvpn", "ovpn", "block", "tg"}
    _PING_UDP_SCHEMES: ClassVar[set[str]] = {"hysteria", "hysteria2", "hy2", "tuic"}
    _PING_MAX_HOSTS: ClassVar[int] = 20
    _PING_MIN_INTERVAL_SECONDS: ClassVar[float] = 30.0
    _ping_last_seen: ClassVar[dict[int, float]] = {}

    @staticmethod
    def _encode_url_header(value: str) -> str:
        if not value or value.isascii():
            return value
        try:
            parts = urlsplit(value)
        except ValueError:
            parts = None
        if parts and parts.scheme and parts.netloc:
            try:
                host = parts.hostname or ""
                if not host.isascii():
                    host = host.encode("idna").decode("ascii")
                if ":" in host and not host.startswith("["):
                    host = f"[{host}]"
                userinfo = ""
                if parts.username:
                    userinfo = quote(parts.username, safe="")
                    if parts.password:
                        userinfo += ":" + quote(parts.password, safe="")
                    userinfo += "@"
                netloc = userinfo + host
                if parts.port is not None:
                    netloc += f":{parts.port}"
                return urlunsplit(
                    (
                        parts.scheme,
                        netloc,
                        quote(parts.path, safe="/%:@!$&'()*+,;="),
                        quote(parts.query, safe="/?%:@!$&'()*+,;="),
                        parts.fragment,
                    )
                )
            except (ValueError, UnicodeError):
                pass
        return quote(value, safe=":/?#[]@!$&'()*+,;=%")

    @staticmethod
    async def build_openvpn_files(user: UsersResponseWithInbounds) -> list[dict]:
        """Per-protocol .ovpn files for the subscription page's download buttons."""
        from app.subscription.share import generate_openvpn_files

        files = await generate_openvpn_files(user)
        out = []
        for filename, content in files.items():
            protocol = filename.rsplit("-", 1)[-1].removesuffix(".ovpn").upper()
            remotes = [line.split()[1] for line in content.splitlines() if line.startswith("remote ")]
            out.append({"filename": filename, "protocol": protocol, "content": content, "remotes": remotes})
        out.sort(key=lambda f: f["protocol"])
        return out

    async def _enforce_ping_rate_limit(self, user_id: int) -> None:
        now = time.monotonic()
        last = self._ping_last_seen.get(user_id)
        if last is not None and now - last < self._PING_MIN_INTERVAL_SECONDS:
            await self.raise_error(message="Too many ping requests", code=429)
        self._ping_last_seen[user_id] = now
        if len(self._ping_last_seen) > 10_000:
            stale = [key for key, seen in self._ping_last_seen.items() if now - seen > 600]
            for key in stale:
                del self._ping_last_seen[key]

    @classmethod
    def _parse_ping_targets(cls, links: list[str]) -> dict[str, tuple[int, bool]]:
        """Parse config links into {host: (port, udp_based)}, one entry per host, skipping non-pingable protocols."""
        targets: dict[str, tuple[int, bool]] = {}
        for raw in links:
            raw = raw.strip()
            m = re.match(r"^([a-z0-9]+)://", raw, re.IGNORECASE)
            if not m:
                continue
            scheme = m.group(1).lower()
            if scheme in cls._PING_SKIP_SCHEMES:
                continue
            udp = scheme in cls._PING_UDP_SCHEMES
            host, port = "", 443
            if scheme == "vmess":
                # vmess payload is base64-encoded JSON: {"add": host, "port": port, ...}
                try:
                    payload = raw[m.end() :].split("#", 1)[0].split("?", 1)[0].strip()
                    data = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))
                    host = str(data.get("add", "")).strip()
                    port = int(str(data.get("port", "443")))
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
            else:
                rest = raw[m.end() :].split("#", 1)[0].split("?", 1)[0]
                if "@" in rest:
                    rest = rest.rsplit("@", 1)[1]
                rest = rest.strip().rstrip("/")
                if rest.startswith("["):  # IPv6 [addr]:port
                    host, _, tail = rest.partition("]")
                    host = host[1:]
                    p = tail.lstrip(":") or "443"
                else:
                    host, _, p = rest.partition(":")
                    p = p or "443"
                host = host.strip()
                try:
                    port = int(p.split("/")[0].split(",")[0])
                except (ValueError, IndexError):
                    port = 443
            if not host or host in targets:
                continue
            targets[host] = (port, udp)
            if len(targets) >= cls._PING_MAX_HOSTS:
                break
        return targets

    @staticmethod
    async def _tcp_ping(host: str, port: int, udp_based: bool = False, timeout: float = 3.0) -> int:
        """TCP handshake latency in ms, or -1 when unreachable / timed out.

        For UDP-based protocols (hysteria2/tuic) a fast TCP "connection refused" still
        proves the host is alive (the kernel answered with RST), so it counts as reachable.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        writer = None
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
            return max(1, round((loop.time() - start) * 1000))
        except ConnectionRefusedError:
            if udp_based:
                return max(1, round((loop.time() - start) * 1000))
            return -1
        except (TimeoutError, OSError, ValueError):
            return -1
        finally:
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass

    async def ping(self, db: AsyncSession, token: str) -> dict[str, int]:
        """Server-side TCP latency/health of the user's config server hosts (no proxy core needed).

        Returns {host: latency_ms} where latency_ms > 0 means reachable and -1 means down/timeout,
        which the subscription page renders as a live per-server status dot + ping badge.
        """
        db_user = await self.get_validated_sub(db, token=token)
        await self._enforce_ping_rate_limit(db_user.id)
        user = await self.validated_user(db_user)
        conf, _ = await self.fetch_config(user, ConfigFormat.links)
        text = conf.decode("utf-8", "ignore") if isinstance(conf, (bytes, bytearray)) else str(conf)
        targets = self._parse_ping_targets([line for line in text.splitlines() if line.strip()])
        if not targets:
            return {}
        items = list(targets.items())
        results = await asyncio.gather(*[self._tcp_ping(host, port, udp) for host, (port, udp) in items])
        return {host: ms for (host, _), ms in zip(items, results)}
