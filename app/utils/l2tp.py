from __future__ import annotations

import secrets
import string
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.wireguard import tags_from_groups
from app.db.models import CoreConfig, CoreType
from app.models.proxy import ProxyTable

L2TP_PASSWORD_LENGTH = 20
_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def generate_l2tp_psk() -> str:
    return secrets.token_urlsafe(24)


def generate_l2tp_password() -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(L2TP_PASSWORD_LENGTH))


def ensure_l2tp_core_material(config: dict) -> dict:
    config = dict(config or {})
    if not str(config.get("psk") or "").strip():
        config["psk"] = generate_l2tp_psk()
    return config


def _core_config_dict(core: CoreConfig) -> dict:
    cfg = core.config or {}
    if isinstance(cfg, str):
        import json

        cfg = json.loads(cfg)
    return cfg


async def get_l2tp_cores(db: AsyncSession) -> list[CoreConfig]:
    result = await db.execute(select(CoreConfig).where(CoreConfig.type == CoreType.l2tp))
    return list(result.scalars().all())


def l2tp_core_tags(cores: Iterable[CoreConfig]) -> set[str]:
    tags: set[str] = set()
    for core in cores:
        tag = str(_core_config_dict(core).get("inbound_tag") or "").strip()
        if tag:
            tags.add(tag)
    return tags


async def user_has_l2tp_access(db: AsyncSession, groups: Iterable) -> bool:
    tags = l2tp_core_tags(await get_l2tp_cores(db))
    return bool(tags and tags & await tags_from_groups(groups))


async def prepare_l2tp_password(
    db: AsyncSession,
    proxy_settings: ProxyTable,
    groups: Iterable,
) -> ProxyTable:
    if not await user_has_l2tp_access(db, groups):
        return proxy_settings

    if not proxy_settings.l2tp.password:
        proxy_settings.l2tp.password = generate_l2tp_password()

    return proxy_settings
