from __future__ import annotations

import hashlib
import string
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import event, inspect, select, tuple_, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.db.models import User
from app.fork.models.proxy_secret_claim import ProxySecretClaim
from app.fork.proxy_secrets.fields import SECRET_FIELD_SPECS, ProxySecretField
from app.fork.proxy_secrets.locks import MYSQL_FAMILY, read_committed_rows
from app.fork.proxy_secrets.uniqueness import ProxySecretConflictError

CLAIM_CHUNK = 400
HEX_DIGITS = frozenset(string.hexdigits)
ROUTE_NORMALIZED_UUID_FIELDS = frozenset({ProxySecretField.vless_id, ProxySecretField.hysteria_auth})
UUID_FIELDS = frozenset({ProxySecretField.vmess_id, ProxySecretField.tuic_uuid})
HEX_FIELDS = frozenset({ProxySecretField.mtproto_secret})

Claims = dict[ProxySecretField, str]
ALL_KINDS = frozenset(spec.field for spec in SECRET_FIELD_SPECS)


@dataclass(frozen=True)
class ClaimChange:
    claims: Claims
    kinds: frozenset
    is_new: bool = False


class ProxySecretClaimConflict(ProxySecretConflictError):
    pass


def uuid_hex(value: str) -> str | None:
    if len(value) == 38 and value[0] == "{" and value[-1] == "}":
        return uuid_hex(value[1:-1])
    if len(value) == 45 and value[:9].lower() == "urn:uuid:":
        return uuid_hex(value[9:])
    if len(value) == 36:
        if any(value[position] != "-" for position in (8, 13, 18, 23)):
            return None
        value = value.replace("-", "")
    if len(value) != 32 or not HEX_DIGITS.issuperset(value):
        return None
    return value.lower()


def canonical_uuid(hex_value: str) -> str:
    return f"{hex_value[:8]}-{hex_value[8:12]}-{hex_value[12:16]}-{hex_value[16:20]}-{hex_value[20:]}"


def normalize_secret(field: ProxySecretField, value: str) -> str:
    if field in ROUTE_NORMALIZED_UUID_FIELDS or field in UUID_FIELDS:
        hex_value = uuid_hex(value)
        if hex_value is None:
            return value
        if field in ROUTE_NORMALIZED_UUID_FIELDS:
            hex_value = hex_value[:12] + "0000" + hex_value[16:]
        return canonical_uuid(hex_value)
    if field in HEX_FIELDS and value and HEX_DIGITS.issuperset(value):
        return value.lower()
    return value


def secret_digest(field: ProxySecretField, value) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return hashlib.sha256(normalize_secret(field, text).encode()).hexdigest()


def stored_claims(proxy_settings) -> Claims:
    claims: Claims = {}
    if not isinstance(proxy_settings, Mapping):
        return claims
    for spec in SECRET_FIELD_SPECS:
        section = proxy_settings.get(spec.protocol)
        if not isinstance(section, Mapping):
            continue
        digest = secret_digest(spec.field, section.get(spec.attribute))
        if digest is not None:
            claims[spec.field] = digest
    return claims


def _chunks(values: Sequence, size: int = CLAIM_CHUNK) -> Iterable[Sequence]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _insert_missing(connection: Connection, rows: list[dict]) -> None:
    table = ProxySecretClaim.__table__
    dialect = connection.dialect.name
    if dialect in MYSQL_FAMILY:
        statement = mysql_insert(table).on_duplicate_key_update(kind=table.c.kind)
    elif dialect == "postgresql":
        statement = pg_insert(table).on_conflict_do_nothing(index_elements=["kind", "digest"])
    else:
        statement = sqlite_insert(table).on_conflict_do_nothing(index_elements=["kind", "digest"])
    for chunk in _chunks(rows):
        connection.execute(statement, list(chunk))


def _held_rows(connection: Connection, keys: list[tuple[str, str]]) -> dict[tuple[str, str], tuple]:
    table = ProxySecretClaim.__table__
    held: dict[tuple[str, str], tuple] = {}
    for chunk in _chunks(keys):
        rows = connection.execute(
            select(table.c.kind, table.c.digest, table.c.user_id, table.c.retired_at)
            .where(tuple_(table.c.kind, table.c.digest).in_(list(chunk)))
            .order_by(table.c.kind, table.c.digest)
            .with_for_update()
        )
        for kind, digest, user_id, retired_at in rows:
            held[(kind, digest)] = (user_id, retired_at)
    return held


def _active_claims(connection: Connection, user_ids: list[int]) -> dict[tuple[int, str], set[str]]:
    table = ProxySecretClaim.__table__
    active: dict[tuple[int, str], set[str]] = {}
    for chunk in _chunks(user_ids):
        rows = connection.execute(
            select(table.c.user_id, table.c.kind, table.c.digest)
            .where(table.c.user_id.in_(list(chunk)), table.c.retired_at.is_(None))
            .order_by(table.c.kind, table.c.digest)
            .with_for_update()
        )
        for user_id, kind, digest in rows:
            active.setdefault((user_id, kind), set()).add(digest)
    return active


def _retire(connection: Connection, stale: dict[str, dict[int, str | None]], now: datetime) -> None:
    table = ProxySecretClaim.__table__
    for kind, kept_by_user in sorted(stale.items()):
        user_ids = sorted(kept_by_user)
        for chunk in _chunks(user_ids):
            kept = sorted(digest for user_id in chunk if (digest := kept_by_user[user_id]) is not None)
            statement = (
                update(table)
                .where(table.c.kind == kind, table.c.user_id.in_(list(chunk)), table.c.retired_at.is_(None))
                .values(retired_at=now)
            )
            if kept:
                statement = statement.where(table.c.digest.not_in(kept))
            connection.execute(statement)


def claim_proxy_secrets(connection: Connection, changes: Mapping[int, ClaimChange]) -> None:
    if not changes:
        return
    now = datetime.now(UTC)
    active = _active_claims(connection, sorted(user_id for user_id, change in changes.items() if not change.is_new))
    wanted: list[tuple[str, str, int]] = []
    stale: dict[str, dict[int, str | None]] = {}
    for user_id, change in changes.items():
        for field in change.kinds:
            kind = field.value
            new = change.claims.get(field)
            current = active.get((user_id, kind), set())
            if new is not None and new not in current:
                wanted.append((kind, new, user_id))
            if current - {new}:
                stale.setdefault(kind, {})[user_id] = new
    wanted.sort()
    if wanted:
        _insert_missing(
            connection,
            [
                {"kind": kind, "digest": digest, "user_id": user_id, "claimed_at": now, "retired_at": None}
                for kind, digest, user_id in wanted
            ],
        )
        held = _held_rows(connection, [(kind, digest) for kind, digest, _ in wanted])
        conflicting = {
            kind
            for kind, digest, user_id in wanted
            if held.get((kind, digest)) is None
            or held[(kind, digest)][0] != user_id
            or held[(kind, digest)][1] is not None
        }
        if conflicting:
            raise ProxySecretClaimConflict(sorted(ProxySecretField(kind) for kind in conflicting))
    _retire(connection, stale, now)


async def foreign_claims(db, wanted: Mapping[ProxySecretField, str], *, user_id: int | None) -> set[ProxySecretField]:
    digests = {field: digest for field, value in wanted.items() if (digest := secret_digest(field, value))}
    if not digests:
        return set()
    table = ProxySecretClaim.__table__
    by_key = {(field.value, digest): field for field, digest in digests.items()}
    rows = await read_committed_rows(
        db,
        select(table.c.kind, table.c.digest, table.c.user_id, table.c.retired_at).where(
            tuple_(table.c.kind, table.c.digest).in_(list(by_key))
        ),
    )
    return {
        by_key[(kind, digest)]
        for kind, digest, holder_id, retired_at in rows
        if (kind, digest) in by_key and (holder_id is None or holder_id != user_id or retired_at is not None)
    }


def _change_for(instance: User, is_new: bool) -> ClaimChange | None:
    if not is_new and not inspect(instance).attrs.proxy_settings.history.has_changes():
        return None
    return ClaimChange(stored_claims(instance.proxy_settings), ALL_KINDS, is_new)


def _changed_users(session: Session) -> dict[int, ClaimChange]:
    changes: dict[int, ClaimChange] = {}
    for instance in (*session.new, *session.dirty):
        if not isinstance(instance, User) or instance.id is None:
            continue
        change = _change_for(instance, instance in session.new)
        if change is not None:
            changes[instance.id] = change
    return changes


@event.listens_for(Session, "after_flush")
def _claim_flushed_proxy_secrets(session: Session, flush_context) -> None:
    holders = _changed_users(session)
    if holders:
        claim_proxy_secrets(session.connection(), holders)


async def claim_stored_proxy_secrets(db, user_ids: Iterable[int], kinds: frozenset | None = None) -> None:
    ordered = sorted(set(user_ids))
    if not ordered:
        return
    kinds = ALL_KINDS if kinds is None else frozenset(kinds)

    def _claim(session: Session) -> None:
        connection = session.connection()
        changes: dict[int, ClaimChange] = {}
        for chunk in _chunks(ordered):
            rows = connection.execute(select(User.id, User.proxy_settings).where(User.id.in_(list(chunk))))
            changes.update({user_id: ClaimChange(stored_claims(settings), kinds) for user_id, settings in rows})
        claim_proxy_secrets(connection, changes)

    await db.run_sync(_claim)



def guarded_field(spec) -> ProxySecretField | None:
    try:
        return ProxySecretField(str(getattr(spec.field, "value", spec.field)))
    except ValueError:
        return None


async def claimed_values(db, field: ProxySecretField, values: Iterable[str]) -> set[str]:
    by_digest = {digest: value for value in values if (digest := secret_digest(field, value))}
    if not by_digest:
        return set()
    table = ProxySecretClaim.__table__
    found: set[str] = set()
    digests = sorted(by_digest)
    for chunk in _chunks(digests):
        rows = await read_committed_rows(
            db, select(table.c.digest).where(table.c.kind == field.value, table.c.digest.in_(list(chunk)))
        )
        found.update(by_digest[row[0]] for row in rows)
    return found
