from __future__ import annotations

import gc
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.sql.selectable import Select

from app.db.models import NodeUserUsage
from app.jobs import record_usages
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401

BUCKET = datetime(2026, 11, 22, 19, 0, tzinfo=UTC)
BATCH_LENGTHS = range(1, 31)
TRAFFIC = 10


def _live_selects() -> int:
    gc.collect()
    return sum(1 for obj in gc.get_objects() if type(obj) is Select)


def _rows(user_ids: list[int], node_id: int, length: int) -> list[dict]:
    return [{"uid": uid, "node_id": node_id, "created_at": BUCKET, "value": TRAFFIC} for uid in user_ids[:length]]


@pytest.mark.asyncio
async def test_writing_usage_batches_of_new_lengths_keeps_the_compiled_cache_and_statements_bounded(recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=max(BATCH_LENGTHS), node_count=1)
    engine = record_usages.engine
    dialect = engine.dialect.name
    cache = engine.sync_engine._compiled_cache
    async with engine.begin() as conn:
        await record_usages.write_node_user_usage_rows(conn, dialect, _rows(user_ids, node_ids[0], 1))
    entries_before = len(cache)
    selects_before = _live_selects()

    for length in BATCH_LENGTHS:
        async with engine.begin() as conn:
            await record_usages.write_node_user_usage_rows(conn, dialect, _rows(user_ids, node_ids[0], length))

    retained_selects = _live_selects() - selects_before
    new_entries = len(cache) - entries_before
    async with recorder_db() as session:
        written = (await session.execute(select(func.sum(NodeUserUsage.used_traffic)))).scalar_one()

    assert written == TRAFFIC + TRAFFIC * sum(BATCH_LENGTHS)
    assert new_entries <= 2, f"{new_entries} compiled-cache entries kept for {len(BATCH_LENGTHS)} batch lengths"
    assert retained_selects < len(BATCH_LENGTHS), f"{retained_selects} Select objects still alive after the writes"


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("postgresql", None),
        ("mysql", {"compiled_cache": None}),
        ("mariadb", {"compiled_cache": None}),
        ("sqlite", {"compiled_cache": None}),
    ],
)
def test_only_the_row_count_shaped_upserts_skip_the_compiled_cache(dialect, expected):
    assert record_usages.node_user_usage_execution_options(dialect) == expected
