"""content filter: claim the sniffing installed before ownership tracking

Revision ID: f6c4d0e2a183
Revises: e5b3c9d1f072
Create Date: 2026-09-15 09:05:00.000000

"""

import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "f6c4d0e2a183"
down_revision = "e5b3c9d1f072"
branch_labels = None
depends_on = None

INSTALLED_SHAPE = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": False}


def _scoped_tags(config: dict) -> tuple[set[str], bool]:
    tags: set[str] = set()
    whole = False
    for rule in (config.get("routing") or {}).get("rules") or []:
        if not str(rule.get("ruleTag") or "").startswith("pgcf-"):
            continue
        bound = rule.get("inboundTag") or []
        if isinstance(bound, str):
            bound = [bound]
        if bound:
            tags.update(str(b) for b in bound)
        else:
            whole = True
    return tags, whole


def upgrade() -> None:
    bind = op.get_bind()
    cores = bind.execute(sa.text("SELECT id, config FROM core_configs")).fetchall()
    existing = {}
    for row in bind.execute(
        sa.text("SELECT core_id, inbound_tag, original FROM content_filter_sniffing_overrides")
    ).fetchall():
        original = row[2]
        if isinstance(original, str):
            try:
                original = json.loads(original)
            except ValueError:
                original = None
        existing[(row[0], row[1])] = original
    now = datetime.now(UTC)
    for core_id, raw in cores:
        config = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        tags, whole = _scoped_tags(config)
        if not tags and not whole:
            continue
        for inbound in config.get("inbounds") or []:
            tag = str(inbound.get("tag") or "")
            if not tag or tag == "API_INBOUND":
                continue
            if not (whole or tag in tags):
                continue
            if inbound.get("sniffing") != INSTALLED_SHAPE:
                continue
            if (core_id, tag) in existing:
                if existing[(core_id, tag)] == INSTALLED_SHAPE:
                    bind.execute(
                        sa.text(
                            "UPDATE content_filter_sniffing_overrides SET original = NULL "
                            "WHERE core_id = :core_id AND inbound_tag = :tag"
                        ),
                        {"core_id": core_id, "tag": tag},
                    )
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO content_filter_sniffing_overrides (core_id, inbound_tag, original, installed_at) "
                    "VALUES (:core_id, :tag, NULL, :now)"
                ),
                {"core_id": core_id, "tag": tag, "now": now},
            )


def downgrade() -> None:
    op.execute("DELETE FROM content_filter_sniffing_overrides WHERE original IS NULL")
