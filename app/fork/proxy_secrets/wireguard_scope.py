from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError
from sqlalchemy import select

from app.db import AsyncSession
from app.db.models import User
from app.models.proxy import ProxyTable, WireGuardSettings
from app.utils.wireguard import user_has_wireguard_access

STORED_WIREGUARD_FIELDS = ("private_key", "public_key", "peer_ips")


def _wireguard_settings(section) -> WireGuardSettings:
    if not isinstance(section, dict):
        return WireGuardSettings()
    try:
        return WireGuardSettings.model_validate(section)
    except ValidationError:
        return WireGuardSettings.model_construct(
            **{key: section[key] for key in STORED_WIREGUARD_FIELDS if section.get(key) is not None}
        )


async def stored_wireguard_settings(db: AsyncSession, user_id: int | None) -> WireGuardSettings:
    if user_id is None:
        return WireGuardSettings()
    stored = await db.scalar(select(User.proxy_settings).where(User.id == user_id).with_for_update())
    return _wireguard_settings(stored.get("wireguard") if isinstance(stored, dict) else None)


async def drop_unentitled_wireguard_keys(
    db: AsyncSession,
    proxy_settings: ProxyTable,
    groups: Iterable,
    *,
    exclude_user_id: int | None = None,
) -> ProxyTable:
    if await user_has_wireguard_access(db, groups):
        return proxy_settings
    proxy_settings.wireguard = await stored_wireguard_settings(db, exclude_user_id)
    return proxy_settings
