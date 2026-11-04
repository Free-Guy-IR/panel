from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DataError, IntegrityError, OperationalError

from app.db.crud.user import reset_user_data_usage
from app.db.models import User
from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    CountingNode,
    recorder_db_fixture,
    seed_users_and_nodes,
    statement_faults_fixture,
    usage_snapshot,
)


class _ConnectionLost(Exception):
    args = (2013, "Lost connection to MySQL server during query")


def is_online_at_write(statement: str) -> bool:
    return " ".join(statement.upper().split()).startswith("UPDATE USERS SET ONLINE_AT=")


async def _two_users_on_one_node(monkeypatch: pytest.MonkeyPatch, recorder_db) -> tuple[list[int], CountingNode]:
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    node = CountingNode(node_ids[0], {user_ids[0]: 300, user_ids[1]: 70})
    monkeypatch.setattr(
        record_usages.node_manager,
        "get_healthy_nodes",
        AsyncMock(return_value=[(node.node_id, node)]),
    )
    return user_ids, node


def _retained_attempts() -> list[tuple[int, int]]:
    return [
        (cohort.attempts, record_usages._raw_bytes(cohort.api_params)) for cohort in record_usages._retained_cohorts
    ]


NON_RETRIABLE_ONLINE_AT_ERRORS = [
    pytest.param(
        DataError("UPDATE users SET online_at", {}, Exception("value out of range for online_at")),
        id="data-error",
    ),
    pytest.param(
        IntegrityError("UPDATE users SET online_at", {}, Exception("CHECK constraint failed: online_at")),
        id="integrity-error",
    ),
    pytest.param(
        OperationalError("UPDATE users SET online_at", {}, Exception("disk I/O error")),
        id="operational-error",
    ),
    pytest.param(
        OperationalError("UPDATE users SET online_at", {}, _ConnectionLost(), connection_invalidated=True),
        id="connection-lost-before-commit",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", NON_RETRIABLE_ONLINE_AT_ERRORS)
async def test_a_non_retriable_online_at_failure_rolls_back_the_round_and_the_next_round_bills_it(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults, caplog, error
):
    user_ids, node = await _two_users_on_one_node(monkeypatch, recorder_db)
    faults = statement_faults(is_online_at_write, error, {1})
    caplog.set_level(logging.WARNING, logger="record-usages")

    with pytest.raises(type(error)):
        await record_usages._record_user_usages_impl()

    assert faults.log.count("billing") >= 1
    assert faults.log.index("billing") < faults.log.index("failed")
    assert node.counters == {user_ids[0]: 0, user_ids[1]: 0}
    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 0, user_ids[1]: 0}
    assert charted == billed
    assert _retained_attempts() == [(1, 370)]
    assert not any("Gave up" in record.getMessage() for record in caplog.records)

    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 300, user_ids[1]: 70}
    assert charted == billed
    assert record_usages._retained_cohorts == []


@pytest.mark.asyncio
async def test_a_round_kept_after_a_dropped_online_at_write_never_bills_a_user_reset_before_its_retry(
    monkeypatch: pytest.MonkeyPatch, recorder_db, statement_faults
):
    user_ids, node = await _two_users_on_one_node(monkeypatch, recorder_db)
    reset_id, kept_id = user_ids
    statement_faults(
        is_online_at_write,
        OperationalError("UPDATE users SET online_at", {}, _ConnectionLost(), connection_invalidated=True),
        {1},
    )

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    assert _retained_attempts() == [(1, 370)]
    async with recorder_db() as session:
        db_user = (await session.execute(select(User).where(User.id == reset_id))).scalar_one()
        await reset_user_data_usage(session, db_user)
    node.add(reset_id, 5)
    node.add(kept_id, 6)

    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {reset_id: 5, kept_id: 76}
    assert charted == billed
    assert record_usages._retained_cohorts == []

    await record_usages._record_user_usages_impl()

    assert await usage_snapshot(recorder_db, user_ids) == (billed, charted)
