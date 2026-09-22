from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.db.crud.user import reset_user_data_usage
from app.db.models import NodeUserUsage, User
from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    CountingNode,
    recorder_db_fixture,
    seed_users_and_nodes,
    usage_snapshot,
)

FROZEN_BUCKET = datetime(2026, 9, 23, 10, 20, tzinfo=UTC)


async def _reset(session_factory, user_id: int, clean_chart_data: bool = False) -> None:
    async with session_factory() as session:
        db_user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        await reset_user_data_usage(session, db_user, clean_chart_data=clean_chart_data)


def _reset_during(monkeypatch: pytest.MonkeyPatch, recorder_db, stage: str, user_id: int, clean_chart_data: bool):
    fired = {"done": False}
    target = "load_user_usage_context" if stage == "after_context_load" else "record_user_stats_batched"
    real = getattr(record_usages, target)

    async def racing(*args, **kwargs):
        result = await real(*args, **kwargs)
        if not fired["done"]:
            fired["done"] = True
            await _reset(recorder_db, user_id, clean_chart_data)
        return result

    monkeypatch.setattr(record_usages, target, racing)
    return fired


def _delta(after: dict[int, int], before: dict[int, int]) -> dict[int, int]:
    return {user_id: after[user_id] - before[user_id] for user_id in after}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["after_context_load", "after_chart_write"])
@pytest.mark.parametrize("clean_chart_data", [False, True])
async def test_chart_rows_follow_the_fence_when_a_user_is_reset_mid_tick(
    monkeypatch: pytest.MonkeyPatch, recorder_db, stage: str, clean_chart_data: bool
):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    reset_user_id, kept_user_id = user_ids
    node = CountingNode(node_ids[0], {reset_user_id: 900, kept_user_id: 70})
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node.node_id, node)]))
    fired = _reset_during(monkeypatch, recorder_db, stage, reset_user_id, clean_chart_data)
    billed_before, charted_before = await usage_snapshot(recorder_db, user_ids)

    await record_usages._record_user_usages_impl()

    assert fired["done"]
    billed_after, charted_after = await usage_snapshot(recorder_db, user_ids)
    billed = _delta(billed_after, billed_before)
    charted = _delta(charted_after, charted_before)
    assert billed[kept_user_id] == charted[kept_user_id] == 70
    assert billed_after[reset_user_id] == 0
    assert charted[reset_user_id] == 0


@pytest.mark.asyncio
async def test_withdrawing_fenced_chart_bytes_keeps_the_bucket_traffic_already_billed(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    monkeypatch.setattr(record_usages, "_get_time_bucket", lambda now=None: FROZEN_BUCKET)
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    user_id = user_ids[0]
    node = CountingNode(node_ids[0], {user_id: 100})
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node.node_id, node)]))

    await record_usages._record_user_usages_impl()
    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed[user_id] == charted[user_id] == 100

    node.add(user_id, 900)
    _reset_during(monkeypatch, recorder_db, "after_chart_write", user_id, clean_chart_data=False)
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed[user_id] == 0
    assert charted[user_id] == 100


@pytest.mark.asyncio
async def test_a_fenced_user_leaves_no_empty_chart_row(monkeypatch: pytest.MonkeyPatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    user_id = user_ids[0]
    node = CountingNode(node_ids[0], {user_id: 450})
    monkeypatch.setattr(record_usages.node_manager, "get_healthy_nodes", AsyncMock(return_value=[(node.node_id, node)]))
    _reset_during(monkeypatch, recorder_db, "after_chart_write", user_id, clean_chart_data=False)

    await record_usages._record_user_usages_impl()

    async with recorder_db() as session:
        rows = (await session.execute(select(NodeUserUsage.id).where(NodeUserUsage.user_id == user_id))).all()
    assert rows == []
