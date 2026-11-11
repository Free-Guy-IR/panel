import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.fork.operation import node_extras
from app.jobs import node_checker
from app.node import NodeManager


@contextlib.contextmanager
def _resync_records():
    records = []

    class Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    sink = Sink(level=logging.DEBUG)
    loggers = [logging.getLogger(name) for name in ("node-checker", "node-operation")]
    previous = [log.level for log in loggers]
    for log in loggers:
        log.addHandler(sink)
        log.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        for log, level in zip(loggers, previous, strict=True):
            log.removeHandler(sink)
            log.setLevel(level)


@contextlib.asynccontextmanager
async def _fake_db():
    yield object()


@pytest.mark.asyncio
async def test_a_resync_refused_by_a_removal_is_logged_below_error(monkeypatch):
    manager = NodeManager()
    manager._removing[1] = 1
    monkeypatch.setattr(node_extras, "node_manager", manager)
    monkeypatch.setattr(node_checker, "GetDB", _fake_db)
    monkeypatch.setattr(node_checker.NodeOperation, "_get_core_users_map", AsyncMock(return_value=({}, {})))

    with _resync_records() as records:
        assert await node_checker._resync_users(SimpleNamespace(id=1, name="n1", core_config_id=1)) is False

    assert records
    assert not [record for record in records if record.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_any_other_resync_failure_is_still_logged_as_an_error(monkeypatch):
    monkeypatch.setattr(node_checker, "GetDB", _fake_db)
    monkeypatch.setattr(node_checker.NodeOperation, "_get_core_users_map", AsyncMock(side_effect=RuntimeError("boom")))

    with _resync_records() as records:
        assert await node_checker._resync_users(SimpleNamespace(id=2, name="n2", core_config_id=1)) is False

    assert [record for record in records if record.levelno >= logging.ERROR]
