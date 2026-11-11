import asyncio
import json
import os
import shutil
import socket
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import nats
import pytest
import pytest_asyncio
from PasarGuardNodeBridge import NodeAPIError
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore

from app.db.models import NodeStatus
from app.node import nats_memory, node_manager
from app.node.nats_memory import NatsNodeLifecycleCoordinator
from tests.api.helpers import unique_name
from tests.test_node_background_connects import SETTLE, Fleet, _queue_on_connect_lock


@pytest_asyncio.fixture
async def two_workers(tmp_path):
    binary = os.environ.get("NATS_SERVER_BINARY") or shutil.which("nats-server")
    if not binary:
        pytest.skip("Set NATS_SERVER_BINARY to run the two-worker lifecycle test against a real JetStream")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = await asyncio.to_thread(
        subprocess.Popen,
        [binary, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(tmp_path / "nats")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    connections = []
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError("Test NATS server exited during startup")
                    await asyncio.sleep(0.02)
        url = f"nats://127.0.0.1:{port}"
        first, second = await nats.connect(url), await nats.connect(url)
        connections = [first, second]
        bucket = "lifecycle_" + uuid4().hex
        first_kv = await first.jetstream().create_key_value(bucket=bucket)
        second_kv = await second.jetstream().key_value(bucket)
        yield SimpleNamespace(
            here=NatsNodeLifecycleCoordinator(first_kv), other=NatsNodeLifecycleCoordinator(second_kv)
        )
    finally:
        for connection in connections:
            await connection.close()
        process.terminate()
        await asyncio.to_thread(process.wait, timeout=10)


async def test_a_late_result_is_skipped_after_another_worker_started_the_node_through_jetstream(
    monkeypatch, two_workers
):
    monkeypatch.setattr(nats_memory, "_user_sync_store", InMemoryUserSyncStore())
    monkeypatch.setattr(nats_memory, "_lifecycle_coordinator", two_workers.here)
    async with Fleet(monkeypatch, shared=True) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        await fleet.set_status(node_id, NodeStatus.error, "before the restart")
        remote.hold("Start")
        await fleet.restart(node_id, [unique_name("bg_b")])

        kwargs = node_manager._create_node_kwargs(await fleet.row(node_id))
        kwargs["user_sync_store"] = InMemoryUserSyncStore()
        kwargs["lifecycle_coordinator"] = two_workers.other
        kwargs["worker_id"] = "the-other-worker"
        other = fleet.create_node(**kwargs)
        with pytest.raises(NodeAPIError) as refused:
            await other.start(config=json.dumps({"tags": fleet.tags}), backend_type=0, users=[])
        assert refused.value.code == 409, refused.value.detail

        lock, taken = await _queue_on_connect_lock(node_id)
        remote.release("Start")
        await asyncio.wait_for(taken, timeout=SETTLE)
        epoch_after_this_worker = (await two_workers.here.get_state(str(node_id))).epoch
        remote.dead = True
        with pytest.raises(NodeAPIError):
            await other.start(config=json.dumps({"tags": fleet.tags}), backend_type=0, users=[])
        remote.dead = False
        assert (await two_workers.here.get_state(str(node_id))).epoch > epoch_after_this_worker
        lock.release()
        await fleet.background_done()

        assert await fleet.status(node_id) is NodeStatus.error, (
            "a late result was written after another worker started the node"
        )


async def test_a_late_result_is_written_when_no_other_worker_touched_the_node_through_jetstream(
    monkeypatch, two_workers
):
    monkeypatch.setattr(nats_memory, "_user_sync_store", InMemoryUserSyncStore())
    monkeypatch.setattr(nats_memory, "_lifecycle_coordinator", two_workers.here)
    async with Fleet(monkeypatch, shared=True) as fleet:
        node_id = await fleet.node()
        remote = fleet.remotes[node_id]
        await fleet.set_status(node_id, NodeStatus.error, "before the restart")
        remote.hold("Start")
        await fleet.restart(node_id, [unique_name("bg_b")])

        remote.release("Start")
        await fleet.background_done()

        assert await fleet.status(node_id) is NodeStatus.connected
        assert fleet.published[-1] == (node_id, NodeStatus.connected)
