from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.wireguard import tags_from_groups
from app.db.models import CoreConfig, CoreType
from app.models.proxy import ProxyTable
from app.utils.system import random_password


def _core_config_dict(core: CoreConfig) -> dict:
    cfg = core.config or {}
    if isinstance(cfg, str):
        import json

        cfg = json.loads(cfg)
    return cfg


async def get_openvpn_cores(db: AsyncSession) -> list[CoreConfig]:
    result = await db.execute(select(CoreConfig).where(CoreConfig.type == CoreType.openvpn))
    return list(result.scalars().all())


def openvpn_core_tags(cores: Iterable[CoreConfig]) -> set[str]:
    """All instance tags across every OpenVPN core - mirrors
    app.utils.mtproto.mtproto_core_tags (an OpenVPN core can have several
    instances, e.g. one udp/1194 and one tcp/443, each its own tag).
    """
    tags: set[str] = set()
    for core in cores:
        for instance in _core_config_dict(core).get("instances") or []:
            tag = str((instance or {}).get("tag") or "").strip()
            if tag:
                tags.add(tag)
    return tags


async def user_has_openvpn_access(db: AsyncSession, groups: Iterable) -> bool:
    ov_tags = openvpn_core_tags(await get_openvpn_cores(db))
    return bool(ov_tags and ov_tags & await tags_from_groups(groups))


async def prepare_openvpn_password(
    db: AsyncSession,
    proxy_settings: ProxyTable,
    groups: Iterable,
) -> ProxyTable:
    """Ensure an OpenVPN password exists for a user assigned to an OpenVPN core.

    Mirrors app.utils.mtproto.prepare_mtproto_secret /
    app.utils.wireguard.prepare_wireguard_keys: only generates (and, by
    being assigned back onto proxy_settings before it's persisted by the
    caller, saves) a password for users who actually have access, and never
    regenerates one that already exists. Pairs with OpenVPNSettings.password
    being `str | None = None` (no bare default_factory) - see that field's
    docstring for the read-time regeneration bug this replaces.
    """
    if not await user_has_openvpn_access(db, groups):
        return proxy_settings

    if not proxy_settings.openvpn.password:
        proxy_settings.openvpn.password = random_password()

    return proxy_settings
