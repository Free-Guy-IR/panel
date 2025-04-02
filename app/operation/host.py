import asyncio

from app.db import AsyncSession
from app.db.models import ProxyHost
from app.models.host import CreateHost, BaseHost
from app.models.admin import AdminDetails
from app.operation import BaseOperator
from app.db.crud import add_host, get_host_by_id, remove_host, get_hosts, modify_host
from app.backend import hosts
from app.utils.logger import get_logger

from app import notification


logger = get_logger("Host-Operator")


class HostOperator(BaseOperator):
    async def get_hosts(self, db: AsyncSession, offset: int = 0, limit: int = 0) -> list[BaseHost]:
        return await get_hosts(db=db, offset=offset, limit=limit)

    async def add_host(self, db: AsyncSession, new_host: CreateHost, admin: AdminDetails) -> BaseHost:
        if (
            new_host.transport_settings
            and new_host.transport_settings.xhttp_settings
            and (nested_host := new_host.transport_settings.xhttp_settings.download_settings)
        ):
            ds_host = await get_host_by_id(db, nested_host)
            if not ds_host:
                return self.raise_error("download host not found", 404)
            if ds_host.transport_settings and ds_host.transport_settings.get("xhttp_settings", {}).get(
                "download_settings"
            ):
                return self.raise_error("download host cannot have a download host", 400)

        await self.check_inbound_tags([new_host.inbound_tag])

        db_host = await add_host(db, new_host)

        logger.info(f'Host "{db_host.id}" added by admin "{admin.username}"')

        host = BaseHost.model_validate(db_host)
        asyncio.create_task(notification.create_host(host, admin.username))

        await hosts.update()

        return host

    async def modify_host(
        self, db: AsyncSession, host_id: int, modified_host: CreateHost, admin: AdminDetails
    ) -> BaseHost:
        if (
            modified_host.transport_settings
            and modified_host.transport_settings.xhttp_settings
            and (nested_host := modified_host.transport_settings.xhttp_settings.download_settings)
        ):
            if nested_host == host_id:
                return self.raise_error("download host cannot be the same as the host", 400)
            ds_host = await get_host_by_id(db, nested_host)
            if not ds_host:
                return self.raise_error("download host not found", 404)
            if ds_host.transport_settings and ds_host.transport_settings.get("xhttp_settings", {}).get(
                "download_settings"
            ):
                return self.raise_error("download host cannot have a download host", 400)

        if modified_host.inbound_tag:
            await self.check_inbound_tags([modified_host.inbound_tag])

        db_host = await self.get_validated_host(db, host_id)

        db_host = await modify_host(db=db, db_host=db_host, modified_host=modified_host)

        logger.info(f'Host "{db_host.id}" modified by admin "{admin.username}"')

        host = BaseHost.model_validate(db_host)
        asyncio.create_task(notification.modify_host(host, admin.username))

        await hosts.update()

        return host

    async def remove_host(self, db: AsyncSession, host_id: int, admin: AdminDetails):
        db_host = await self.get_validated_host(db, host_id)
        await remove_host(db, db_host)
        logger.info(f'Host "{db_host.id}" deleted by admin "{admin.username}"')

        host = BaseHost.model_validate(db_host)

        asyncio.create_task(notification.remove_host(host, admin.username))

        await hosts.update()

    async def update_hosts(
        self, db: AsyncSession, modified_hosts: list[CreateHost], admin: AdminDetails
    ) -> list[BaseHost]:
        for host in modified_hosts:
            old_host: ProxyHost | None = None
            if host.id is not None:
                old_host = await get_host_by_id(db, host.id)

            if old_host is None:
                await add_host(db, host)
            else:
                await modify_host(db, old_host, host)

        await hosts.update()

        logger.info(f'Host\'s has been modified by admin "{admin.username}"')

        asyncio.create_task(notification.update_hosts(admin.username))

        return await get_hosts(db=db)
