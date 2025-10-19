from asyncio import Lock
from copy import deepcopy

from sqlalchemy.ext.asyncio import AsyncSession
from aiocache import cached

from app import on_startup
from app.core.manager import core_manager
from app.db import GetDB
from app.db.crud.host import get_host_by_id, get_hosts, upsert_inbounds
from app.db.models import ProxyHostSecurity
from app.models.host import BaseHost


def _prepare_host_data(host: BaseHost) -> dict:
    return {
        "remark": host.remark,
        "inbound_tag": host.inbound_tag,
        "address": [v for v in host.address],
        "port": host.port,
        "path": host.path or None,
        "sni": [v for v in host.sni] if host.sni else [],
        "host": [v for v in host.host] if host.host else [],
        "alpn": [alpn.value for alpn in host.alpn] if host.alpn else [],
        "fingerprint": host.fingerprint.value,
        "tls": None if host.security == ProxyHostSecurity.inbound_default else host.security.value,
        "allowinsecure": host.allowinsecure,
        "fragment_settings": host.fragment_settings.model_dump() if host.fragment_settings else None,
        "noise_settings": host.noise_settings.model_dump() if host.noise_settings else None,
        "random_user_agent": host.random_user_agent,
        "use_sni_as_host": host.use_sni_as_host,
        "http_headers": host.http_headers,
        "mux_settings": host.mux_settings.model_dump(by_alias=True, exclude_none=True) if host.mux_settings else {},
        "transport_settings": host.transport_settings.model_dump(by_alias=True, exclude_none=True)
        if host.transport_settings
        else {},
        "status": host.status,
        "ech_config_list": host.ech_config_list,
        "priority": host.priority,
    }


class HostManager:
    def __init__(self):
        self._hosts = {}
        self._lock = Lock()

    async def setup(self, db: AsyncSession):
        db_hosts = await get_hosts(db)
        await self.add_hosts(db, db_hosts)

    async def _reset_cache(self):
        await self.get_hosts.cache.clear()

    @staticmethod
    async def _prepare_host_entry(
        db: AsyncSession, host: BaseHost, inbounds_list: list[str]
    ) -> tuple[int, dict] | None:
        if host.is_disabled or (host.inbound_tag not in inbounds_list):
            return None

        downstream = None
        if (
            host.transport_settings
            and host.transport_settings.xhttp_settings
            and (ds_host := host.transport_settings.xhttp_settings.download_settings)
        ):
            downstream = await get_host_by_id(db, ds_host)

        host_data = _prepare_host_data(host)

        if downstream:
            host_data["downloadSettings"] = _prepare_host_data(downstream)
        else:
            host_data["downloadSettings"] = None

        return (host.id, host_data)

    async def add_host(self, db: AsyncSession, host: BaseHost):
        await self.add_hosts(db, [host])

    async def add_hosts(self, db: AsyncSession, hosts: list[BaseHost]):
        serialized_hosts = [BaseHost.model_validate(host) for host in hosts]
        inbounds_list = await core_manager.get_inbounds()
        await upsert_inbounds(db, inbounds_list)
        await db.commit()

        prepared_hosts = []
        hosts_to_remove = []
        for host in serialized_hosts:
            result = await self._prepare_host_entry(db, host, inbounds_list)
            if result:
                prepared_hosts.append(result)
            else:
                hosts_to_remove.append(host.id)

        # Acquire lock only for updating the dict and cache
        async with self._lock:
            for host_id, host_data in prepared_hosts:
                self._hosts[host_id] = host_data

            for host_id in hosts_to_remove:
                self._hosts.pop(host_id, None)

            await self._reset_cache()

    async def remove_host(self, id: int):
        async with self._lock:
            self._hosts.pop(id, None)
            await self._reset_cache()

    async def get_host(self, id: int) -> dict | None:
        async with self._lock:
            return deepcopy(self._hosts.get(id))

    @cached()
    async def get_hosts(self) -> dict[int, dict]:
        async with self._lock:
            # Return hosts sorted by priority
            sorted_hosts = dict(sorted(self._hosts.items(), key=lambda x: x[1]["priority"]))
            return deepcopy(sorted_hosts)


host_manager: HostManager = HostManager()


@on_startup
async def initialize_hosts():
    async with GetDB() as db:
        await host_manager.setup(db)
