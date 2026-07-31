from __future__ import annotations

import secrets as _secrets
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.wireguard import tags_from_groups
from app.db.models import CoreConfig, CoreType
from app.models.proxy import ProxyTable

_MTPROTO_SECRET_BYTES = 16


def _core_config_dict(core: CoreConfig) -> dict:
    cfg = core.config or {}
    if isinstance(cfg, str):
        import json

        cfg = json.loads(cfg)
    return cfg


async def get_mtproto_cores(db: AsyncSession) -> list[CoreConfig]:
    result = await db.execute(select(CoreConfig).where(CoreConfig.type == CoreType.mtproto))
    return list(result.scalars().all())


def mtproto_core_tags(cores: Iterable[CoreConfig]) -> set[str]:
    """All instance tags across every MTProto core - unlike WireGuard (one
    interface/tag per core), an MTProto core can have several instances
    (e.g. one fake-TLS domain on 443, another on 8443), each its own tag.
    """
    tags: set[str] = set()
    for core in cores:
        for instance in _core_config_dict(core).get("instances") or []:
            tag = str((instance or {}).get("tag") or "").strip()
            if tag:
                tags.add(tag)
    return tags


async def user_has_mtproto_access(db: AsyncSession, groups: Iterable) -> bool:
    mt_tags = mtproto_core_tags(await get_mtproto_cores(db))
    return bool(mt_tags and mt_tags & await tags_from_groups(groups))


def generate_mtproto_secret() -> str:
    return _secrets.token_hex(_MTPROTO_SECRET_BYTES)


async def prepare_mtproto_secret(
    db: AsyncSession,
    proxy_settings: ProxyTable,
    groups: Iterable,
) -> ProxyTable:
    """Ensure an MTProto secret exists for a user assigned to an MTProto core.

    Mirrors app.utils.wireguard.prepare_wireguard_keys: only generates
    (and, by being assigned back onto proxy_settings before it's persisted
    by the caller, saves) a secret for users who actually have access,
    and never regenerates one that already exists.
    """
    if not await user_has_mtproto_access(db, groups):
        return proxy_settings

    if not proxy_settings.mtproto.secret:
        proxy_settings.mtproto.secret = generate_mtproto_secret()

    return proxy_settings
