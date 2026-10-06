import logging
from collections import defaultdict
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "e79833e2a0b2"
down_revision = "fd192e9628a5"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

USER_CHUNK = 1000
CLAIM_CHUNK = 500

users = sa.table("users", sa.column("id", sa.BigInteger()), sa.column("proxy_settings", sa.JSON()))


def _claims_table():
    return sa.table(
        "proxy_secret_claims",
        sa.column("kind", sa.String(32)),
        sa.column("digest", sa.String(64)),
        sa.column("user_id", sa.BigInteger()),
        sa.column("claimed_at", sa.DateTime(timezone=True)),
        sa.column("retired_at", sa.DateTime(timezone=True)),
    )


def _load_users(bind) -> dict[int, dict]:
    loaded: dict[int, dict] = {}
    last_id = None
    while True:
        query = sa.select(users.c.id, users.c.proxy_settings).order_by(users.c.id).limit(USER_CHUNK)
        if last_id is not None:
            query = query.where(users.c.id > last_id)
        rows = bind.execute(query).all()
        if not rows:
            return loaded
        for user_id, settings in rows:
            loaded[user_id] = settings if isinstance(settings, dict) else {}
        last_id = rows[-1][0]


def _unique_replacement(spec, taken: set[str], secret_digest) -> tuple[str, str]:
    while True:
        value = str(spec.generate())
        digest = secret_digest(spec.field, value)
        if digest not in taken:
            taken.add(digest)
            return value, digest


def _backfill(bind) -> None:
    from app.fork.proxy_secrets.claims import secret_digest
    from app.fork.proxy_secrets.fields import SECRET_FIELD_SPECS

    now = datetime.now(UTC)
    settings_by_user = _load_users(bind)
    holders: dict[tuple[str, str], list[int]] = defaultdict(list)
    missing: dict[str, int] = defaultdict(int)
    for user_id, settings in settings_by_user.items():
        for spec in SECRET_FIELD_SPECS:
            section = settings.get(spec.protocol)
            value = section.get(spec.attribute) if isinstance(section, dict) else None
            digest = secret_digest(spec.field, value)
            if digest is None:
                missing[spec.field.value] += 1
                continue
            holders[(spec.field.value, digest)].append(user_id)

    taken: dict[str, set[str]] = defaultdict(set)
    for kind, digest in holders:
        taken[kind].add(digest)

    specs = {spec.field.value: spec for spec in SECRET_FIELD_SPECS}
    rows: list[dict] = []
    rotated_users: dict[int, dict] = {}
    groups: dict[str, int] = defaultdict(int)
    rotated: dict[str, int] = defaultdict(int)
    for (kind, digest), user_ids in sorted(holders.items()):
        if len(user_ids) == 1:
            rows.append({"kind": kind, "digest": digest, "user_id": user_ids[0], "claimed_at": now, "retired_at": None})
            continue
        groups[kind] += 1
        rows.append({"kind": kind, "digest": digest, "user_id": None, "claimed_at": now, "retired_at": now})
        spec = specs[kind]
        for user_id in user_ids:
            value, new_digest = _unique_replacement(spec, taken[kind], secret_digest)
            settings = rotated_users.get(user_id) or dict(settings_by_user[user_id])
            section = dict(settings.get(spec.protocol) or {})
            section[spec.attribute] = value
            settings[spec.protocol] = section
            rotated_users[user_id] = settings
            rotated[kind] += 1
            rows.append({"kind": kind, "digest": new_digest, "user_id": user_id, "claimed_at": now, "retired_at": None})

    for user_id, settings in sorted(rotated_users.items()):
        bind.execute(sa.update(users).where(users.c.id == user_id).values(proxy_settings=settings))

    claims = _claims_table()
    for start in range(0, len(rows), CLAIM_CHUNK):
        bind.execute(sa.insert(claims), rows[start : start + CLAIM_CHUNK])

    logger.warning(
        "proxy secret claims: %d user(s) scanned, %d claim row(s) written; shared-value groups per kind %s; "
        "users given a new value per kind %s (%d user(s) in total); users without a value per kind %s",
        len(settings_by_user),
        len(rows),
        dict(sorted(groups.items())),
        dict(sorted(rotated.items())),
        len(rotated_users),
        dict(sorted(missing.items())),
    )


def upgrade() -> None:
    op.create_table(
        "proxy_secret_claims",
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("user_id", SqliteCompatibleBigInteger(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_proxy_secret_claims_user_id_users", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("kind", "digest", name="pk_proxy_secret_claims"),
    )
    op.create_index("ix_proxy_secret_claims_user_id", "proxy_secret_claims", ["user_id"])
    _backfill(op.get_bind())


def downgrade() -> None:
    op.execute("DELETE FROM proxy_secret_claims")
    op.drop_index("ix_proxy_secret_claims_user_id", table_name="proxy_secret_claims")
    op.drop_table("proxy_secret_claims")
