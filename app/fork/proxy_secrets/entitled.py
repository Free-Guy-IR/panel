from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field as dataclass_field
from enum import StrEnum

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from app.db import AsyncSession
from app.db.crud.wireguard import get_users_accessible_tags
from app.db.models import CoreConfig, CoreType, User
from app.fork.proxy_secrets.uniqueness import (
    MAX_REGENERATION_ATTEMPTS,
    ProxySecretUniquenessError,
    ReservedSecrets,
    secret_column,
    write_stored_secret,
)
from app.models.proxy import L2TPSettings, MTProtoSettings, OpenVPNSettings
from app.utils.l2tp import generate_l2tp_password, l2tp_core_tags
from app.utils.logger import get_logger
from app.utils.mtproto import generate_mtproto_secret, mtproto_core_tags
from app.utils.openvpn import openvpn_core_tags
from app.utils.system import random_password

logger = get_logger("entitled-secrets")

LOOKUP_CHUNK = 500


class EntitledSecretField(StrEnum):
    openvpn_password = "openvpn.password"
    l2tp_password = "l2tp.password"
    mtproto_secret = "mtproto.secret"


@dataclass(frozen=True)
class EntitledSecretSpec:
    field: EntitledSecretField
    protocol: str
    attribute: str
    label: str
    core_type: CoreType
    settings_model: type[BaseModel]
    core_tags: Callable[[Iterable[CoreConfig]], set[str]]
    generate: Callable[[], str]


ENTITLED_SECRET_SPECS: tuple[EntitledSecretSpec, ...] = (
    EntitledSecretSpec(
        EntitledSecretField.openvpn_password,
        "openvpn",
        "password",
        "OpenVPN",
        CoreType.openvpn,
        OpenVPNSettings,
        openvpn_core_tags,
        random_password,
    ),
    EntitledSecretSpec(
        EntitledSecretField.l2tp_password,
        "l2tp",
        "password",
        "L2TP",
        CoreType.l2tp,
        L2TPSettings,
        l2tp_core_tags,
        generate_l2tp_password,
    ),
    EntitledSecretSpec(
        EntitledSecretField.mtproto_secret,
        "mtproto",
        "secret",
        "MTProto",
        CoreType.mtproto,
        MTProtoSettings,
        mtproto_core_tags,
        generate_mtproto_secret,
    ),
)

ENTITLED_SPEC_BY_FIELD: dict[EntitledSecretField, EntitledSecretSpec] = {
    spec.field: spec for spec in ENTITLED_SECRET_SPECS
}


def entitled_specs(fields=None) -> list[EntitledSecretSpec]:
    wanted = None if fields is None else {EntitledSecretField(value) for value in fields}
    return [ENTITLED_SPEC_BY_FIELD[member] for member in EntitledSecretField if wanted is None or member in wanted]


def _core_type_value(core_type) -> str:
    return str(getattr(core_type, "value", core_type))


def spec_for_core_type(core_type) -> EntitledSecretSpec | None:
    wanted = _core_type_value(core_type)
    for spec in entitled_specs():
        if spec.core_type.value == wanted:
            return spec
    return None


def stored_secret(proxy_settings, spec: EntitledSecretSpec) -> str | None:
    section = proxy_settings.get(spec.protocol) if isinstance(proxy_settings, dict) else None
    if section is None:
        return None
    value = getattr(spec.settings_model.model_validate(section), spec.attribute)
    return str(value) if value else None


async def entitled_core_tags(
    db: AsyncSession, specs: Sequence[EntitledSecretSpec]
) -> dict[EntitledSecretField, set[str]]:
    if not specs:
        return {}
    core_types = [spec.core_type for spec in specs]
    cores = list((await db.execute(select(CoreConfig).where(CoreConfig.type.in_(core_types)))).scalars().all())
    return {
        spec.field: spec.core_tags([core for core in cores if _core_type_value(core.type) == spec.core_type.value])
        for spec in specs
    }


async def secret_values_in_use(db: AsyncSession, spec: EntitledSecretSpec, values: Iterable[str]) -> set[str]:
    column = secret_column(spec)
    ordered = sorted(set(values))
    found: set[str] = set()
    for start in range(0, len(ordered), LOOKUP_CHUNK):
        chunk = ordered[start : start + LOOKUP_CHUNK]
        rows = await db.execute(select(column).where(column.in_(chunk)))
        found.update(row[0] for row in rows if row[0])
    return found


async def issue_unique_secrets(
    db: AsyncSession,
    spec: EntitledSecretSpec,
    count: int,
    reserved: ReservedSecrets | None = None,
) -> list[str]:
    taken = reserved.setdefault(spec.field, set()) if reserved is not None else set()
    issued = [""] * count
    pending = list(range(count))
    for _ in range(MAX_REGENERATION_ATTEMPTS + 1):
        for index in pending:
            issued[index] = str(spec.generate())
        in_use = await secret_values_in_use(db, spec, (issued[index] for index in pending))
        collided = []
        for index in pending:
            value = issued[index]
            if value in in_use or value in taken:
                collided.append(index)
            else:
                taken.add(value)
        if not collided:
            return issued
        logger.warning(
            f"{len(collided)} freshly generated {spec.field.value} value(s) were already taken; generating new ones"
        )
        pending = collided

    error = ProxySecretUniquenessError([spec.field])
    logger.error(str(error))
    raise error


@dataclass
class SecretPlan:
    grants: list[tuple[User, list[EntitledSecretSpec]]] = dataclass_field(default_factory=list)
    unreadable: list[tuple[int, EntitledSecretField]] = dataclass_field(default_factory=list)


def plan_entitled_secrets(
    users: Iterable[User],
    tags_by_user: dict[int, set[str]],
    tags_by_field: dict[EntitledSecretField, set[str]],
    specs: Sequence[EntitledSecretSpec],
) -> SecretPlan:
    plan = SecretPlan()
    for user in users:
        user_tags = tags_by_user.get(user.id) or set()
        missing = []
        for spec in specs:
            if not tags_by_field.get(spec.field, set()) & user_tags:
                continue
            try:
                current = stored_secret(user.proxy_settings, spec)
            except ValidationError:
                plan.unreadable.append((user.id, spec.field))
                continue
            if current is None:
                missing.append(spec)
        if missing:
            plan.grants.append((user, missing))
    return plan


def report_unreadable(plan: SecretPlan) -> None:
    if plan.unreadable:
        logger.warning(
            f"left {len(plan.unreadable)} unreadable stored secret(s) untouched instead of overwriting them: "
            f"{[(user_id, field.value) for user_id, field in plan.unreadable[:20]]}"
        )


async def apply_secret_plan(
    db: AsyncSession,
    plan: SecretPlan,
    reserved: ReservedSecrets | None = None,
) -> list[User]:
    holders: dict[EntitledSecretField, list[User]] = {}
    for user, specs in plan.grants:
        for spec in specs:
            holders.setdefault(spec.field, []).append(user)

    for secret_field, users in holders.items():
        spec = ENTITLED_SPEC_BY_FIELD[secret_field]
        values = await issue_unique_secrets(db, spec, len(users), reserved)
        for user, value in zip(users, values, strict=True):
            user.proxy_settings = write_stored_secret(user.proxy_settings, spec, value)

    return [user for user, _ in plan.grants]


async def grant_entitled_secrets(
    db: AsyncSession,
    users: Sequence[User],
    *,
    fields=None,
    reserved: ReservedSecrets | None = None,
) -> list[User]:
    if not users:
        return []
    specs = entitled_specs(fields)
    tags_by_field = await entitled_core_tags(db, specs)
    reachable_tags = set().union(*tags_by_field.values()) if tags_by_field else set()
    if not reachable_tags:
        return []

    tags_by_user: dict[int, set[str]] = {}
    user_ids = sorted({user.id for user in users})
    for start in range(0, len(user_ids), LOOKUP_CHUNK):
        tags_by_user.update(await get_users_accessible_tags(db, user_ids[start : start + LOOKUP_CHUNK]))
    plan = plan_entitled_secrets(users, tags_by_user, tags_by_field, specs)
    report_unreadable(plan)
    changed = await apply_secret_plan(db, plan, reserved)
    if changed:
        await db.flush()
        logger.info(f"issued missing entitled secrets to {len(changed)} user(s) after an access change")
    return changed
