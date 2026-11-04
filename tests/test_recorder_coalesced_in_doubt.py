from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.jobs import record_usages
from tests.test_recorder_harness import (  # noqa: F401
    recorder_db_fixture,
    seed_users_and_nodes,
    usage_snapshot,
)
from tests.test_recorder_two_phase_drain import TwoPhaseNode, _ConnectionGone, _install


class _RolledBack(Exception):
    pass


@pytest.fixture(autouse=True)
def coalesced_history(monkeypatch: pytest.MonkeyPatch, recorder_db) -> None:
    monkeypatch.setattr(record_usages, "_history_is_coalesced", lambda dialect: True)


@pytest.mark.asyncio
async def test_a_coalesced_commit_in_doubt_that_landed_charts_its_bytes_once(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=1, node_count=1)
    uid = user_ids[0]
    node = TwoPhaseNode({uid: 1_234})
    _install(monkeypatch, [(node_ids[0], node)])
    real = record_usages.run_in_retried_transaction
    raised = {"done": False}

    async def commits_then_reports_a_dropped_connection(operation, max_retries=record_usages.DEADLOCK_MAX_RETRIES):
        result = await real(operation, max_retries)
        if not raised["done"]:
            raised["done"] = True
            raise OperationalError("COMMIT", {}, _ConnectionGone(), connection_invalidated=True)
        return result

    monkeypatch.setattr(record_usages, "run_in_retried_transaction", commits_then_reports_a_dropped_connection)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 1_234}
    assert charted == billed

    await record_usages._record_user_usages_impl()
    node.add(uid, 6)
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {uid: 1_240}
    assert charted == billed


@pytest.mark.asyncio
async def test_a_coalesced_commit_in_doubt_that_never_landed_is_charted_once_when_billed(monkeypatch, recorder_db):
    _, user_ids, node_ids = await seed_users_and_nodes(recorder_db, user_count=2, node_count=1)
    node = TwoPhaseNode({user_ids[0]: 300, user_ids[1]: 70})
    _install(monkeypatch, [(node_ids[0], node)])
    real = record_usages.run_in_retried_transaction
    raised = {"done": False}

    async def rolls_back_then_reports_a_dropped_connection(operation, max_retries=record_usages.DEADLOCK_MAX_RETRIES):
        if raised["done"]:
            return await real(operation, max_retries)
        raised["done"] = True

        async def run_then_roll_back(conn):
            await operation(conn)
            raise _RolledBack

        with pytest.raises(_RolledBack):
            await real(run_then_roll_back, max_retries)
        raise OperationalError("COMMIT", {}, _ConnectionGone(), connection_invalidated=True)

    monkeypatch.setattr(record_usages, "run_in_retried_transaction", rolls_back_then_reports_a_dropped_connection)

    with pytest.raises(OperationalError):
        await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 0, user_ids[1]: 0}
    assert charted == billed
    assert not record_usages._pending_user_usage_history

    await record_usages._record_user_usages_impl()
    await record_usages._record_user_usages_impl()

    billed, charted = await usage_snapshot(recorder_db, user_ids)
    assert billed == {user_ids[0]: 300, user_ids[1]: 70}
    assert charted == billed
