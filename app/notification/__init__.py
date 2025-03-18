import asyncio

from . import discord as ds
from . import telegram as tg
from app.models.host import BaseHost


async def add_host(host: BaseHost, by: str):
    asyncio.gather(ds.add_host(host, by), tg.add_host(host, by))


async def modify_host(host: BaseHost, by: str):
    asyncio.gather(ds.modify_host(host, by), tg.modify_host(host, by))


async def remove_host(host: BaseHost, by: str):
    asyncio.gather(ds.remove_host(host, by), tg.remove_host(host, by))


async def update_hosts(by: str):
    asyncio.gather(ds.update_hosts(by), tg.update_hosts(by))
