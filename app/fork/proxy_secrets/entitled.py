from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field as dataclass_field
from enum import StrEnum

from pydantic import BaseModel, ValidationError
from sqlalchemy import String, Text, and_, case, cast, func, literal, or_, select, update
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary
from sqlalchemy.dialects.postgresql import JSON as PostgresJSON, JSONB
from sqlalchemy.orm.attributes import set_committed_value

from app.db import AsyncSession
from app.db.crud.wireguard import get_users_accessible_tags
from app.db.models import CoreConfig, CoreType, User
from app.fork.proxy_secrets.uniqueness import (
    MAX_REGENERATION_ATTEMPTS,
    ProxySecretUniquenessError,
    ReservedSecrets,
    secret_column,
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


class UnguardableValue(ValueError):
    pass


def replaceable_value(proxy_settings, spec: EntitledSecretSpec) -> str | None:
    section = proxy_settings.get(spec.protocol) if isinstance(proxy_settings, dict) else None
    raw = section.get(spec.attribute) if isinstance(section, dict) else None
    if raw is not None and not isinstance(raw, str):
        raise UnguardableValue(spec.field.value)
    return raw


def _sqlite_put(column, spec: EntitledSecretSpec, expected: str | None, value: str):
    section_path = f"$.{spec.protocol}"
    key_path = f"{section_path}.{spec.attribute}"
    current_type = func.json_type(column, key_path)
    if expected is None:
        guard = or_(current_type.is_(None), current_type == "null")
    else:
        guard = and_(current_type == "text", func.json_extract(column, key_path) == expected)
    new_document = case(
        (func.json_type(column, section_path) == "object", func.json_set(column, key_path, value)),
        else_=func.json_set(column, section_path, func.json_object(spec.attribute, value)),
    )
    return guard, new_document


def _mysql_put(column, spec: EntitledSecretSpec, expected: str | None, value: str):
    section_path = f"$.{spec.protocol}"
    key_path = f"{section_path}.{spec.attribute}"
    current = func.json_extract(column, key_path)
    current_type = func.json_type(current)
    if expected is None:
        guard = func.coalesce(current_type, "NULL") == "NULL"
    else:
        guard = and_(
            current_type == "STRING",
            cast(func.json_unquote(current), MySQLBinary()) == cast(literal(expected, String), MySQLBinary()),
        )
    new_document = case(
        (func.json_type(func.json_extract(column, section_path)) == "OBJECT", func.json_set(column, key_path, value)),
        else_=func.json_set(column, section_path, func.json_object(spec.attribute, value)),
    )
    return guard, new_document


def _postgresql_put(column, spec: EntitledSecretSpec, expected: str | None, value: str):
    document = cast(column, JSONB)
    section = document.op("->", return_type=JSONB)(literal(spec.protocol, Text))
    current = section.op("->", return_type=JSONB)(literal(spec.attribute, Text))
    if expected is None:
        guard = or_(current.is_(None), func.jsonb_typeof(current) == "null")
    else:
        guard = and_(
            func.jsonb_typeof(current) == "string",
            section.op("->>", return_type=Text)(literal(spec.attribute, Text)) == expected,
        )
    base = case((func.jsonb_typeof(section) == "object", section), else_=func.jsonb_build_object(type_=JSONB))
    new_section = base.op("||", return_type=JSONB)(
        func.jsonb_build_object(literal(spec.attribute, Text), cast(literal(value, Text), Text), type_=JSONB)
    )
    new_document = document.op("||", return_type=JSONB)(
        func.jsonb_build_object(literal(spec.protocol, Text), new_section, type_=JSONB)
    )
    return guard, cast(new_document, PostgresJSON)


_PUT_BUILDERS = {
    "sqlite": _sqlite_put,
    "mysql": _mysql_put,
    "mariadb": _mysql_put,
    "postgresql": _postgresql_put,
}


def conditional_secret_update(dialect_name: str, spec: EntitledSecretSpec, user_id: int, expected, value: str):
    builder = _PUT_BUILDERS.get(dialect_name)
    if builder is None:
        raise NotImplementedError(f"no conditional JSON update is defined for the {dialect_name!r} dialect")
    table = User.__table__
    guard, new_document = builder(table.c.proxy_settings, spec, expected, value)
    return update(table).where(table.c.id == user_id, guard).values(proxy_settings=new_document)


async def put_entitled_secret(
    db: AsyncSession, spec: EntitledSecretSpec, user_id: int, expected: str | None, value: str
) -> bool:
    statement = conditional_secret_update(db.get_bind().dialect.name, spec, user_id, expected, value)
    result = await db.execute(statement)
    return result.rowcount == 1


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
    grants: list[tuple[User, list[tuple[EntitledSecretSpec, str | None]]]] = dataclass_field(default_factory=list)
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
                if stored_secret(user.proxy_settings, spec) is not None:
                    continue
                missing.append((spec, replaceable_value(user.proxy_settings, spec)))
            except ValidationError, UnguardableValue:
                plan.unreadable.append((user.id, spec.field))
        if missing:
            plan.grants.append((user, missing))
    return plan


def report_unreadable(plan: SecretPlan) -> None:
    if plan.unreadable:
        logger.warning(
            f"left {len(plan.unreadable)} unreadable stored secret(s) untouched instead of overwriting them: "
            f"{[(user_id, field.value) for user_id, field in plan.unreadable[:20]]}"
        )


async def _reload_proxy_settings(db: AsyncSession, users: Sequence[User]) -> None:
    by_id = {user.id: user for user in users}
    user_ids = sorted(by_id)
    for start in range(0, len(user_ids), LOOKUP_CHUNK):
        chunk = user_ids[start : start + LOOKUP_CHUNK]
        rows = await db.execute(
            select(User.id, User.proxy_settings).where(User.id.in_(chunk)).order_by(User.id).with_for_update()
        )
        for user_id, proxy_settings in rows.all():
            set_committed_value(by_id[user_id], "proxy_settings", proxy_settings)


async def apply_secret_plan(
    db: AsyncSession,
    plan: SecretPlan,
    reserved: ReservedSecrets | None = None,
) -> list[User]:
    if not plan.grants:
        return []
    await db.flush()

    ordered = sorted(plan.grants, key=lambda grant: grant[0].id)
    holders: dict[EntitledSecretField, list[int]] = {}
    for user, missing in ordered:
        for spec, _ in missing:
            holders.setdefault(spec.field, []).append(user.id)

    issued: dict[tuple[int, EntitledSecretField], str] = {}
    for secret_field, user_ids in holders.items():
        values = await issue_unique_secrets(db, ENTITLED_SPEC_BY_FIELD[secret_field], len(user_ids), reserved)
        issued.update(zip(((user_id, secret_field) for user_id in user_ids), values, strict=True))

    written: set[int] = set()
    skipped = 0
    for user, missing in ordered:
        for spec, expected in missing:
            if await put_entitled_secret(db, spec, user.id, expected, issued[(user.id, spec.field)]):
                written.add(user.id)
            else:
                skipped += 1

    await _reload_proxy_settings(db, [user for user, _ in plan.grants])
    if skipped:
        logger.info(f"{skipped} entitled secret(s) were set by someone else meanwhile and were left as they are")
    return [user for user, _ in plan.grants if user.id in written]


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
        logger.info(f"issued missing entitled secrets to {len(changed)} user(s) after an access change")
    return changed
