from __future__ import annotations

from sqlalchemy import or_, select

from app.db import AsyncSession
from app.db.models import User
from app.fork.proxy_secrets.fields import SPEC_BY_FIELD, ProxySecretField, SecretFieldSpec, specs_for
from app.models.proxy import ProxyTable
from app.utils.logger import get_logger

logger = get_logger("proxy-secret-uniqueness")

MAX_REGENERATION_ATTEMPTS = 5


def secret_column(spec: SecretFieldSpec):
    return User.proxy_settings[spec.protocol][spec.attribute].as_string()


def read_secret(proxy_settings: ProxyTable, spec: SecretFieldSpec) -> str | None:
    section = getattr(proxy_settings, spec.protocol, None)
    if section is None:
        return None
    value = getattr(section, spec.attribute, None)
    if value is None:
        return None
    return str(value) or None


def write_secret(proxy_settings: ProxyTable, spec: SecretFieldSpec, value) -> None:
    setattr(getattr(proxy_settings, spec.protocol), spec.attribute, value)


def read_stored_secret(proxy_settings, spec: SecretFieldSpec) -> str | None:
    if not isinstance(proxy_settings, dict):
        return None
    section = proxy_settings.get(spec.protocol)
    if not isinstance(section, dict):
        return None
    value = section.get(spec.attribute)
    if value is None:
        return None
    return str(value) or None


def write_stored_secret(proxy_settings: dict, spec: SecretFieldSpec, value) -> dict:
    settings = dict(proxy_settings or {})
    section = settings.get(spec.protocol)
    section = dict(section) if isinstance(section, dict) else {}
    section[spec.attribute] = str(value)
    settings[spec.protocol] = section
    return settings


async def secret_holder_counts(
    db: AsyncSession,
    wanted: dict[ProxySecretField, str],
    *,
    exclude_user_id: int | None = None,
) -> dict[ProxySecretField, int]:
    if not wanted:
        return {}

    specs = [SPEC_BY_FIELD[field] for field in wanted]
    columns = [secret_column(spec) for spec in specs]
    matches = [column == wanted[spec.field] for column, spec in zip(columns, specs, strict=True)]

    stmt = select(User.id, *columns).where(or_(*matches))
    if exclude_user_id is not None:
        stmt = stmt.where(User.id != exclude_user_id)

    counts: dict[ProxySecretField, int] = {}
    for row in (await db.execute(stmt)).all():
        for offset, spec in enumerate(specs, start=1):
            if row[offset] is not None and row[offset] == wanted[spec.field]:
                counts[spec.field] = counts.get(spec.field, 0) + 1
    return counts


class ProxySecretUniquenessError(ValueError):
    def __init__(self, fields) -> None:
        self.fields = sorted(field.value for field in fields)
        super().__init__(
            "could not establish unique proxy secrets after "
            f"{MAX_REGENERATION_ATTEMPTS} attempts for: {', '.join(self.fields)}"
        )


ReservedSecrets = dict[ProxySecretField, set[str]]


def reserved_secret_collisions(
    pending: dict[ProxySecretField, str],
    reserved: ReservedSecrets | None,
) -> set[ProxySecretField]:
    if not reserved:
        return set()
    return {field for field, value in pending.items() if value in reserved.get(field, ())}


def reserve_secret_values(proxy_settings: ProxyTable, reserved: ReservedSecrets | None, *, fields=None) -> None:
    if reserved is None:
        return
    for spec in specs_for(fields):
        value = read_secret(proxy_settings, spec)
        if value:
            reserved.setdefault(spec.field, set()).add(value)


async def enforce_unique_proxy_secrets(
    db: AsyncSession,
    proxy_settings: ProxyTable,
    *,
    exclude_user_id: int | None = None,
    fields=None,
    reserved: ReservedSecrets | None = None,
) -> ProxyTable:
    pending: dict[ProxySecretField, str] = {}
    for spec in specs_for(fields):
        value = read_secret(proxy_settings, spec)
        if value:
            pending[spec.field] = value

    for regenerations_done in range(MAX_REGENERATION_ATTEMPTS + 1):
        stored_holders = await secret_holder_counts(db, pending, exclude_user_id=exclude_user_id)
        batch_holders = reserved_secret_collisions(pending, reserved)
        if not stored_holders and not batch_holders:
            reserve_secret_values(proxy_settings, reserved, fields=fields)
            return proxy_settings

        if regenerations_done == MAX_REGENERATION_ATTEMPTS:
            break

        pending = {}
        for field in dict.fromkeys((*stored_holders, *batch_holders)):
            spec = SPEC_BY_FIELD[field]
            if field in stored_holders:
                logger.warning(
                    f"proxy secret {field.value} submitted for this user is already stored by "
                    f"{stored_holders[field]} other user(s); replacing it with a freshly generated unique value"
                )
            else:
                logger.warning(
                    f"proxy secret {field.value} submitted for this user was already taken by an earlier user "
                    "in the same request; replacing it with a freshly generated unique value"
                )
            replacement = spec.generate()
            write_secret(proxy_settings, spec, replacement)
            pending[field] = str(replacement)

    error = ProxySecretUniquenessError(pending)
    logger.error(str(error))
    raise error
