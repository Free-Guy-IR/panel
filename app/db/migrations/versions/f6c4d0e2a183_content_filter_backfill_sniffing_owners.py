"""content filter: claim verified legacy sniffing installations

Revision ID: f6c4d0e2a183
Revises: e5b3c9d1f072
Create Date: 2026-09-15 09:05:00.000000

"""

import json
import os
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

revision = "f6c4d0e2a183"
down_revision = "e5b3c9d1f072"
branch_labels = None
depends_on = None

LEDGER = "content_filter_sniffing_repairs"
OVERRIDES = "content_filter_sniffing_overrides"
MAPPING_ENV = "CONTENT_FILTER_LEGACY_OWNED"


def _verified_pairs() -> set[tuple[int, str]]:
    raw = os.environ.get(MAPPING_ENV, "").strip()
    pairs: set[tuple[int, str]] = set()
    for item in filter(None, (piece.strip() for piece in raw.split(";"))):
        core, _, tag = item.partition(":")
        if core.strip().isdigit() and tag.strip():
            pairs.add((int(core.strip()), tag.strip()))
    return pairs


def _decode(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def upgrade() -> None:
    op.create_table(
        LEDGER,
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("core_id", SqliteCompatibleBigInteger(), nullable=False),
        sa.Column("inbound_tag", sa.String(length=256), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("previous_original", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{LEDGER}"),
    )

    pairs = _verified_pairs()
    if not pairs:
        return

    bind = op.get_bind()
    now = datetime.now(UTC)
    existing = {
        (row[0], row[1]): _decode(row[2])
        for row in bind.execute(sa.text(f"SELECT core_id, inbound_tag, original FROM {OVERRIDES}")).fetchall()
    }
    known_cores = {row[0] for row in bind.execute(sa.text("SELECT id FROM core_configs")).fetchall()}

    for core_id, tag in sorted(pairs):
        if core_id not in known_cores:
            continue
        if (core_id, tag) in existing:
            previous = existing[(core_id, tag)]
            if previous is None:
                continue
            bind.execute(
                sa.text(f"UPDATE {OVERRIDES} SET original = NULL WHERE core_id = :core_id AND inbound_tag = :tag"),
                {"core_id": core_id, "tag": tag},
            )
            bind.execute(
                sa.text(
                    f"INSERT INTO {LEDGER} (core_id, inbound_tag, action, previous_original, applied_at) "
                    "VALUES (:core_id, :tag, 'rewritten', :previous, :now)"
                ),
                {"core_id": core_id, "tag": tag, "previous": json.dumps(previous), "now": now},
            )
        else:
            bind.execute(
                sa.text(
                    f"INSERT INTO {OVERRIDES} (core_id, inbound_tag, original, installed_at) "
                    "VALUES (:core_id, :tag, NULL, :now)"
                ),
                {"core_id": core_id, "tag": tag, "now": now},
            )
            bind.execute(
                sa.text(
                    f"INSERT INTO {LEDGER} (core_id, inbound_tag, action, previous_original, applied_at) "
                    "VALUES (:core_id, :tag, 'inserted', NULL, :now)"
                ),
                {"core_id": core_id, "tag": tag, "now": now},
            )


def downgrade() -> None:
    bind = op.get_bind()
    entries = bind.execute(
        sa.text(f"SELECT core_id, inbound_tag, action, previous_original FROM {LEDGER} ORDER BY id DESC")
    ).fetchall()
    for core_id, tag, action, previous in entries:
        if action == "inserted":
            bind.execute(
                sa.text(f"DELETE FROM {OVERRIDES} WHERE core_id = :core_id AND inbound_tag = :tag AND original IS NULL"),
                {"core_id": core_id, "tag": tag},
            )
        elif action == "rewritten":
            bind.execute(
                sa.text(f"UPDATE {OVERRIDES} SET original = :previous WHERE core_id = :core_id AND inbound_tag = :tag"),
                {"core_id": core_id, "tag": tag, "previous": previous if isinstance(previous, str) else json.dumps(previous)},
            )
    op.drop_table(LEDGER)
