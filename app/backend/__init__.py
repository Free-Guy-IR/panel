from copy import deepcopy

from asyncio import Lock

from .xray import XRayConfig
from .abstract_backend import AbstractBackend
from app import on_startup
from app.db import GetDB
from app.db.models import BackendConfig
from app.db.crud import get_backend_configs


class BackendManager:
    def __init__(self):
        self._backends: dict[int, AbstractBackend] = {}
        self._lock = Lock()
        self._inbounds: list[str] = []
        self._inbounds_by_tag = {}

    async def update_backend(self, db_backend_config: BackendConfig):
        fallbacks_inbound_tags = (
            db_backend_config.fallbacks_inbound_tags.split(",") if db_backend_config.fallbacks_inbound_tags else []
        )
        exclude_inbound_tags = (
            db_backend_config.exclude_inbound_tags.split(",") if db_backend_config.exclude_inbound_tags else []
        )

        backend_config = XRayConfig(db_backend_config.config, fallbacks_inbound_tags, exclude_inbound_tags)

        async with self._lock:
            self._backends.update({db_backend_config.id: backend_config})
            self._inbounds_by_tag.update(backend_config.inbounds_by_tag)
            self._inbounds = list(self._inbounds_by_tag.keys())

    async def remove_backend(self, backend_id: int):
        async with self._lock:
            backend = self._backends.get(backend_id, None)
            if backend:
                del self._backends[backend_id]
            else:
                return

            for backend in self._backends.values():
                self._inbounds_by_tag.update(backend.inbounds_by_tag)

            self._inbounds = list(self._inbounds_by_tag.keys())

    async def get_backend(self, backend_id: int) -> AbstractBackend | None:
        async with self._lock:
            backend = self._backends.get(backend_id, None)

            if not backend:
                backend = self._backends.get(1)

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


backend_manager = BackendManager()


@on_startup
async def init_backend_manager():
    async with GetDB() as db:
        backend_configs, _ = await get_backend_configs(db)

        for config in backend_configs:
            await backend_manager.update_backend(config)


__all__ = ["config", "XRayConfig"]
