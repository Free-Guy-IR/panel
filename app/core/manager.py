from asyncio import Lock
from copy import deepcopy

from app import on_startup
from app.core.abstract_backend import AbstractBackend
from app.core.xray import XRayConfig
from app.db import GetDB
from app.db.crud import get_core_configs
from app.db.models import CoreConfig


class CoreManager:
    def __init__(self):
        self._cores: dict[int, AbstractBackend] = {}
        self._lock = Lock()
        self._inbounds: list[str] = []
        self._inbounds_by_tag = {}

    async def update_core(self, db_core_config: CoreConfig):
        fallbacks_inbound_tags = (
            db_core_config.fallbacks_inbound_tags.split(",") if db_core_config.fallbacks_inbound_tags else []
        )
        exclude_inbound_tags = (
            db_core_config.exclude_inbound_tags.split(",") if db_core_config.exclude_inbound_tags else []
        )

        backend_config = XRayConfig(db_core_config.config, fallbacks_inbound_tags, exclude_inbound_tags)

        async with self._lock:
            self._cores.update({db_core_config.id: backend_config})
            self._inbounds_by_tag.update(backend_config.inbounds_by_tag)
            self._inbounds = list(self._inbounds_by_tag.keys())

    async def remove_core(self, core_id: int):
        async with self._lock:
            backend = self._cores.get(core_id, None)
            if backend:
                del self._cores[core_id]
            else:
                return

            for backend in self._cores.values():
                self._inbounds_by_tag.update(backend.inbounds_by_tag)

            self._inbounds = list(self._inbounds_by_tag.keys())

    async def get_core(self, backend_id: int) -> AbstractBackend | None:
        async with self._lock:
            backend = self._cores.get(backend_id, None)

            if not backend:
                backend = self._cores.get(1)

            return backend

    async def get_inbounds(self) -> list[str]:
        async with self._lock:
            return deepcopy(self._inbounds)

    async def get_inbounds_by_tag(self) -> dict:
        async with self._lock:
            return deepcopy(self._inbounds_by_tag)

    async def get_inbound_by_tag(self, tag) -> dict:
        async with self._lock:
            inbound = self._inbounds_by_tag.get(tag, None)
            if not inbound:
                return None
            return deepcopy(inbound)


core_manager = CoreManager()


@on_startup
async def init_core_manager():
    async with GetDB() as db:
        core_configs, _ = await get_core_configs(db)

        for config in core_configs:
            await core_manager.update_core(config)
