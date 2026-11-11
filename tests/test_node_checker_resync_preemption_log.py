import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.jobs import node_checker
from app.node import UserSyncPreemptedError


@contextlib.contextmanager
def _checker_records():
    records = []

    class Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    sink = Sink(level=logging.DEBUG)
    log = logging.getLogger("node-checker")
    previous = log.level
    log.addHandler(sink)
    log.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        log.removeHandler(sink)
        log.setLevel(previous)


@contextlib.asynccontextmanager
async def _fake_db():
    yield object()


@pytest.mark.parametrize(
    ("error", "expect_error_level"),
    [(UserSyncPreemptedError("removed"), False), (RuntimeError("boom"), True)],
    ids=["preempted", "other_failure"],
)
@pytest.mark.asyncio
async def test_resync_logs_a_preemption_below_error(monkeypatch, error, expect_error_level):
    monkeypatch.setattr(node_checker, "GetDB", _fake_db)
    monkeypatch.setattr(node_checker.NodeOperation, "_resync_node_users", AsyncMock(side_effect=error))

    with _checker_records() as records:
        assert await node_checker._resync_users(SimpleNamespace(id=1, name="n1")) is False

    assert records
    assert any(record.levelno >= logging.ERROR for record in records) is expect_error_level
