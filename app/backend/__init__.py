from app import on_startup
from app.utils.store import DictStorage
from .xray import XRayConfig
from app.db import GetDB
from app.db.models import ProxyHostSecurity
from app.db.crud import get_hosts, get_or_create_inbound
from config import XRAY_JSON


config = XRayConfig(XRAY_JSON)


@DictStorage
async def hosts(storage: dict):
    storage.clear()
    async with GetDB() as db:
        db_hosts = await get_hosts(db)

        for host in db_hosts:
            if host.is_disabled or (config.get_inbound(host.inbound_tag) is None):
                continue

            storage[host.id] = {
                "remark": host.remark,
                "inbound_tag": host.inbound_tag,
                "address": [i.strip() for i in host.address.split(",")] if host.address else [],
                "port": host.port,
                "path": host.path if host.path else None,
                "sni": [i.strip() for i in host.sni.split(",")] if host.sni else [],
                "host": [i.strip() for i in host.host.split(",")] if host.host else [],
                "alpn": host.alpn.value,
                "fingerprint": host.fingerprint.value,
                # None means the tls is not specified by host itself and
                #  complies with its inbound's settings.
                "tls": None if host.security == ProxyHostSecurity.inbound_default else host.security.value,
                "allowinsecure": host.allowinsecure,
                "fragment_settings": host.fragment_settings,
                "noise_settings": host.noise_settings,
                "random_user_agent": host.random_user_agent,
                "use_sni_as_host": host.use_sni_as_host,
                "http_headers": host.http_headers,
                "mux_settings": host.mux_settings,
                "transport_settings": host.transport_settings,
                "status": host.status,
            }


async def check_inbounds():
    async with GetDB() as db:
        for tag in config.inbounds:
            await get_or_create_inbound(db, tag)


on_startup(hosts.update)
on_startup(check_inbounds)


__all__ = ["config", "hosts", "nodes", "XRayConfig"]
