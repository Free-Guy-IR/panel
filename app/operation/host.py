import asyncio

from app.operation import BaseOperator

from app.utils.logger import get_logger
from app.db import Session
from app.db.models import ProxyHost
from app.db.crud import add_host, get_or_create_inbound, get_host_by_id, remove_host, get_hosts, modify_host
from app.models.host import CreateHost, BaseHost
from app.models.admin import Admin
from app.backend import hosts
from app import notification


logger = get_logger("Host-Operator")


class HostOperator(BaseOperator):
    @staticmethod
    async def create_db_host(db: Session, new_host: CreateHost) -> ProxyHost:
        inbound = get_or_create_inbound(db, new_host.inbound_tag)

        return ProxyHost(inbound=inbound, **new_host.model_dump(exclude={"inbound_tag", "id"}))

    async def get_hosts(
        self,
        db: Session,
        offset: int = 0,
        limit: int = 0,
    ) -> list[BaseHost]:
        return get_hosts(db=db, offset=offset, limit=limit)

    async def add_host(
        self,
        db: Session,
        new_host: CreateHost,
        admin: Admin,
    ) -> BaseHost:
        db_host = add_host(db, await self.create_db_host(db, new_host))

        logger.info(f'Host "{db_host.id}" added by admin "{admin.username}"')

        host = BaseHost.model_validate(db_host)

        asyncio.create_task(notification.add_host(host, admin.username))

        hosts.update()

        return host

    async def modify_host(
        self,
        db: Session,
        host_id: int,
        modified_host: CreateHost,
        admin: Admin,
    ) -> BaseHost:
        db_host: ProxyHost = await self.get_validated_host(db, host_id)

        db_host = modify_host(db=db, db_host=db_host, modified_host=await self.create_db_host(db, modified_host))

        logger.info(f'Host "{db_host.id}" modified by admin "{admin.username}"')

        host = BaseHost.model_validate(db_host)

        asyncio.create_task(notification.modify_host(host, admin.username))

        hosts.update()

        return host

    async def remove_host(
        self,
        db: Session,
        host_id: int,
        admin: Admin,
    ):
        db_host: ProxyHost = await self.get_validated_host(db, host_id)
        remove_host(db, db_host)
        logger.info(f'Host "{db_host.id}" deleted by admin "{admin.username}"')

        host = BaseHost.model_validate(db_host)

        asyncio.create_task(notification.remove_host(host, admin.username))

        hosts.update()

    async def update_hosts(self, db: Session, modified_hosts: list[CreateHost], admin: Admin) -> list[BaseHost]:
        for host in modified_hosts:
            db_host = await self.create_db_host(db, host)

            old_host: ProxyHost | None = None
            if host.id is not None:
                old_host = get_host_by_id(db, host.id)

            if old_host is None:
                add_host(db, db_host)
            else:
                modify_host(db, old_host, db_host)

        logger.info(f'Host\'s has been modified by admin "{admin.username}"')

        asyncio.create_task(notification.update_hosts(admin.username))

        return get_hosts(db=db)
