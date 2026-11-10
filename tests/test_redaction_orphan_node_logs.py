import asyncio
import contextlib
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PasarGuardNodeBridge import NodeAPIError

from app.jobs import node_checker
from app.node import worker as worker_module

PASSWORD = "Pr0xy" + "S3cret"
LEAK = f"Cannot connect to proxy socks5://user:{PASSWORD}@proxy.example:1080"


def _logged(caplog) -> str:
    return "\n".join(
        f"{record.getMessage()}\n{logging.Formatter().formatException(record.exc_info) if record.exc_info else ''}"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_a_failed_node_loop_never_logs_the_proxy_password(caplog, monkeypatch):
    calls = 0

    async def failing():
        nonlocal calls
        calls += 1
        raise RuntimeError(LEAK)

    async def stop_after_one(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(node_checker.asyncio, "sleep", stop_after_one)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(asyncio.CancelledError):
        await node_checker._interval_loop(failing, 1, "health")
    assert calls == 1
    assert "health" in _logged(caplog)
    assert PASSWORD not in _logged(caplog)


@pytest.mark.asyncio
async def test_a_failed_node_command_never_logs_the_proxy_password(caplog):
    service = worker_module.NodeWorkerService()
    service._dispatch_command = AsyncMock(side_effect=RuntimeError(LEAK))
    caplog.set_level(logging.DEBUG)
    await service._run_command("connect_node", {})
    assert "connect_node" in _logged(caplog)
    assert PASSWORD not in _logged(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["item", "raise"])
async def test_a_streamed_log_error_never_publishes_the_proxy_password(monkeypatch, failure):
    queue: asyncio.Queue = asyncio.Queue()

    @contextlib.asynccontextmanager
    async def stream():
        if failure == "raise":
            raise RuntimeError(LEAK)
        await queue.put(NodeAPIError(-1, LEAK))
        yield queue

    monkeypatch.setattr(
        worker_module.node_manager, "get_node", AsyncMock(return_value=SimpleNamespace(stream_logs=stream))
    )
    service = worker_module.NodeWorkerService()
    published: list[bytes] = []

    async def publish(subject, data):
        published.append(data)

    service._nc = SimpleNamespace(
        publish=publish, subscribe=AsyncMock(return_value=SimpleNamespace(unsubscribe=AsyncMock()))
    )
    reply = await service._start_logs({"node_id": 1})
    await asyncio.wait_for(service._log_tasks.get(reply["subject"]) or asyncio.sleep(0), 2)
    assert published
    assert all(PASSWORD.encode() not in data for data in published)
