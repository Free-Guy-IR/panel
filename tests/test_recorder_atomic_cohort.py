from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.db.crud.user import reset_user_data_usage
from app.db.models import NodeUserUsage, User
from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    CountingNode,
    is_billing_write,
    is_chart_write,
    recorder_db_fixture,
    seed_users_and_nodes,
    statement_faults_fixture,
    usage_snapshot,
)


class _ConnectionGone(Exception):
    args = (2003, "Can't connect to MySQL server")


class _ConnectionLost(Exception):
    args = (2013, "Lost connection to MySQL server during query")


def _install(monkeypatch: pytest.MonkeyPatch, nodes: list[CountingNode]) -> None:
    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(node.node_id, node) for node in nodes]),
    )


async def _chart_row_count(session_factory, user_ids: list[int]) -> int:
    async with session_factory() as session:
        return int(
            (
                await session.execute(
                    select(func.count()).select_from(NodeUserUsage).where(NodeUserUsage.user_id.in_(user_ids))
                )
            ).scalar_one()
        )


def _retained_attempts() -> list[tuple[int, int]]:
    return [
        (cohort.attempts, record_usages._raw_bytes(cohort.api_params)) for cohort in record_usages._retained_cohorts
    ]


def _gave_up(caplog, *fragments: str) -> bool:
    return any(
        record.levelno == logging.ERROR and all(fragment in record.getMessage() for fragment in fragments)
        for record in caplog.records
    )


async def _two_users_on_one_node(monkeypatch: pytest.MonkeyPatch, recorder_db) -> list[int]:
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    _install(monkeypatch, [CountingNode(node_ids[0], {user_ids[0]: 300, user_ids[1]: 70})])
    return user_ids


async def _assert_rolled_back_then_billed_once(recorder_db, user_ids: list[int]) -> None:
    assert await _chart_row_count(recorder_db, user_ids) == 0
    billed, _ = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 0, user_ids[1]: 0}
    assert _retained_attempts() == [(1, 370)]

    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 300, user_ids[1]: 70}
    assert charted == billed
    assert record_usages._retained_cohorts == []


@pytest.mark.asyncio
async def test_a_billing_failure_after_the_chart_write_rolls_back_the_whole_cohort(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults
):
    user_ids = await _two_users_on_one_node(monkeypatch, recorder_db)
    faults = statement_faults(is_billing_write, OperationalError("UPDATE users", {}, _ConnectionGone()), {1})

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert faults.log[: faults.log.index("failed")].count("chart") >= 1
    await _assert_rolled_back_then_billed_once(recorder_db, user_ids)


@pytest.mark.asyncio
async def test_a_failed_second_chart_chunk_rolls_back_the_first(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults
):
    user_ids = await _two_users_on_one_node(monkeypatch, recorder_db)
    monkeypatch.setattr(
        record_usages, "NODE_USER_USAGE_BATCH_SIZE_BY_DIALECT", {"mysql": 1, "sqlite": 1, "postgresql": 1}
    )
    faults = statement_faults(is_chart_write, OperationalError("INSERT", {}, _ConnectionGone()), {2})

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert faults.log[: faults.log.index("failed")].count("chart") == 2
    await _assert_rolled_back_then_billed_once(recorder_db, user_ids)


@pytest.mark.asyncio
async def test_a_fenced_user_never_gets_a_chart_row(monkeypatch: pytest.MonkeyPatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    fenced_id, kept_id = user_ids
    _install(monkeypatch, [CountingNode(node_ids[0], {fenced_id: 900, kept_id: 70})])
    built_uids: list[int] = []
    real_build = record_usages.build_node_user_usage_upsert
    real_context = record_usages.load_user_usage_context
    fired = {"done": False}

    def recording_build(dialect, upsert_params):
        built_uids.extend(int(param["uid"]) for param in upsert_params)
        return real_build(dialect, upsert_params)

    async def context_then_reset(uids):
        context = await real_context(uids)
        if not fired["done"]:
            fired["done"] = True
            async with recorder_db() as session:
                db_user = (await session.execute(select(User).where(User.id == fenced_id))).scalar_one()
                await reset_user_data_usage(session, db_user)
        return context

    monkeypatch.setattr(record_usages, "build_node_user_usage_upsert", recording_build)
    monkeypatch.setattr(record_usages, "load_user_usage_context", context_then_reset)

    await record_usages._record_user_usages_impl()

    assert fired["done"]
    assert fenced_id not in built_uids
    assert kept_id in built_uids
    assert await _chart_row_count(recorder_db, [fenced_id]) == 0
    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {fenced_id: 0, kept_id: 70}
    assert charted == billed


@pytest.mark.asyncio
async def test_a_cohort_that_always_fails_is_retried_up_to_the_bound_then_abandoned(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults, caplog
):
    user_ids = await _two_users_on_one_node(monkeypatch, recorder_db)
    monkeypatch.setattr(record_usages, "USAGE_RETAINED_MAX_ATTEMPTS", 3)
    statement_faults(is_billing_write, OperationalError("UPDATE users", {}, _ConnectionGone()))
    caplog.set_level(logging.WARNING, logger="record-usages")

    retained_after_each_tick = []
    for _ in range(3):
        with pytest.raises(OperationalError):
            await record_usages._record_user_usages_impl()
        retained_after_each_tick.append(_retained_attempts())

    assert retained_after_each_tick == [[(1, 370)], [(2, 370)], []]
    assert _gave_up(caplog, "Gave up on 370 raw bytes", "after 3 attempt(s)", "the retry limit was reached")
    assert await _chart_row_count(recorder_db, user_ids) == 0
    billed, _ = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 0, user_ids[1]: 0}


@pytest.mark.asyncio
async def test_a_write_whose_connection_dropped_is_abandoned_at_once(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults, caplog
):
    user_ids = await _two_users_on_one_node(monkeypatch, recorder_db)
    statement_faults(
        is_billing_write,
        OperationalError("UPDATE users", {}, _ConnectionLost(), connection_invalidated=True),
        {1},
    )
    caplog.set_level(logging.WARNING, logger="record-usages")

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert record_usages._retained_cohorts == []
    assert _gave_up(caplog, "Gave up on 370 raw bytes", "may have committed")
    assert not any("Still holding" in record.getMessage() for record in caplog.records)

    await record_usages._record_user_usages_impl()

    billed, _ = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 0, user_ids[1]: 0}
