import asyncio

import pytest

from app.core import hosts as hosts_module
from app.core.hosts import HostManager
from app.models.host import BaseHost
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.nats.message import MessageTopic, NatsMessage
from app.nats.router import NatsMessageRouter

HOST_ID = 41


def _inbound(remark: str) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark=remark,
        inbound_tag="in",
        protocol="vless",
        network="tcp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(),
        priority=1,
    )


class _Session:
    async def commit(self):
        return None


class _Delivery:
    def __init__(self, data: bytes):
        self.data = data


class _Bus:
    def __init__(self):
        self.routers: list[NatsMessageRouter] = []
        self.held = False
        self.in_flight: list[bytes] = []

    def join(self, worker: HostManager) -> None:
        worker_router = NatsMessageRouter()
        worker_router.register_handler(MessageTopic.HOST, worker._handle_host_message)
        self.routers.append(worker_router)

    async def publish(self, topic: MessageTopic, data: dict, *, raise_on_error: bool = False):
        raw = NatsMessage(topic=topic, data=data).model_dump_json().encode()
        if self.held:
            self.in_flight.append(raw)
            return
        await self._deliver(raw)

    async def _deliver(self, raw: bytes) -> None:
        for worker_router in list(self.routers):
            await worker_router._dispatch(_Delivery(raw))

    async def deliver_in_flight(self) -> None:
        while self.in_flight:
            await self._deliver(self.in_flight.pop(0))


class _Gate:
    def __init__(self, remark: str):
        self.remark = remark
        self.armed = True
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()


@pytest.fixture
def database() -> dict[int, str]:
    return {}


@pytest.fixture
def gate() -> _Gate:
    return _Gate("w1")


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch, database: dict[int, str], gate: _Gate) -> _Bus:
    shared_bus = _Bus()

    async def get_inbounds():
        return ["in"]

    async def upsert_inbounds(db, inbounds):
        return None

    async def read_hosts(db, host_ids):
        return [
            BaseHost(id=host_id, remark=database[host_id], priority=1, inbound_tag="in")
            for host_id in host_ids
            if host_id in database
        ]

    async def prepare(self, db, host, inbounds_list):
        if host.remark == gate.remark and gate.armed:
            gate.armed = False
            gate.paused.set()
            await gate.resume.wait()
        if host.remark.startswith("disabled"):
            return None
        return host.id, _inbound(host.remark)

    monkeypatch.setattr(hosts_module, "is_nats_enabled", lambda: True)
    monkeypatch.setattr(hosts_module, "is_multi_worker", lambda: True)
    monkeypatch.setattr(hosts_module, "router", shared_bus)
    monkeypatch.setattr(hosts_module, "upsert_inbounds", upsert_inbounds)
    monkeypatch.setattr(hosts_module.core_manager, "get_inbounds", get_inbounds)
    monkeypatch.setattr(HostManager, "_read_hosts", staticmethod(read_hosts))
    monkeypatch.setattr(HostManager, "_prepare_host_entry", prepare)
    return shared_bus


def _worker(bus: _Bus) -> HostManager:
    worker = HostManager()
    assert worker._add_hosts_impl == worker._add_hosts_nats
    bus.join(worker)
    return worker


async def _write(worker: HostManager, database: dict[int, str], remark: str) -> None:
    database[HOST_ID] = remark
    await worker.add_hosts(_Session(), [BaseHost(id=HOST_ID, remark=remark, priority=1, inbound_tag="in")])


async def _paused_older_write_then_newer_write(
    writer: HostManager, database: dict[int, str], gate: _Gate, older: str, newer: str
) -> None:
    gate.remark = older
    older_write = asyncio.create_task(_write(writer, database, older))
    await asyncio.wait_for(gate.paused.wait(), 5)
    await _write(writer, database, newer)
    gate.resume.set()
    await asyncio.wait_for(older_write, 5)


async def _cached_remark(worker: HostManager) -> str | None:
    host = await worker.get_host(HOST_ID)
    return None if host is None else host["remark"]


@pytest.mark.asyncio
async def test_own_echo_of_a_paused_older_add_does_not_replace_the_newer_cache_entry(bus, database, gate):
    worker = _worker(bus)

    await _paused_older_write_then_newer_write(worker, database, gate, "w1", "w2")

    assert database[HOST_ID] == "w2"
    assert await _cached_remark(worker) == "w2"


@pytest.mark.asyncio
async def test_own_echo_of_a_paused_older_remove_does_not_drop_the_newer_cache_entry(bus, database, gate):
    worker = _worker(bus)
    await _write(worker, database, "w0")

    await _paused_older_write_then_newer_write(worker, database, gate, "disabled-w1", "w2")

    assert database[HOST_ID] == "w2"
    assert await _cached_remark(worker) == "w2"


@pytest.mark.asyncio
async def test_two_workers_never_serve_an_older_write_after_a_newer_one(bus, database, gate):
    writer = _worker(bus)
    reader = _worker(bus)

    await _paused_older_write_then_newer_write(writer, database, gate, "w1", "w2")

    assert database[HOST_ID] == "w2"
    assert await _cached_remark(writer) == "w2"
    assert await _cached_remark(reader) == "w2"

    await _write(reader, database, "w3")
    assert await _cached_remark(writer) == "w3"
    assert await _cached_remark(reader) == "w3"

    gate.armed = True
    gate.paused.clear()
    gate.resume.clear()
    await _paused_older_write_then_newer_write(reader, database, gate, "disabled-w4", "w5")
    assert database[HOST_ID] == "w5"
    assert await _cached_remark(writer) == "w5"
    assert await _cached_remark(reader) == "w5"

    await writer.remove_host(HOST_ID)
    del database[HOST_ID]
    assert await _cached_remark(writer) is None
    assert await _cached_remark(reader) is None


async def _remove(worker: HostManager, database: dict[int, str]) -> None:
    database.pop(HOST_ID, None)
    await worker.remove_host(HOST_ID)


async def _paused_write(worker: HostManager, database: dict[int, str], gate: _Gate, remark: str) -> asyncio.Task:
    gate.remark = remark
    gate.armed = True
    gate.paused.clear()
    gate.resume.clear()
    write = asyncio.create_task(_write(worker, database, remark))
    await asyncio.wait_for(gate.paused.wait(), 5)
    return write


@pytest.mark.asyncio
async def test_two_workers_converge_on_the_last_write_in_nats_order(bus, database, gate):
    first = _worker(bus)
    second = _worker(bus)
    bus.held = True

    await _write(first, database, "v1")
    await _write(second, database, "v2")
    await bus.deliver_in_flight()

    assert database[HOST_ID] == "v2"
    assert await _cached_remark(first) == "v2"
    assert await _cached_remark(second) == "v2"


@pytest.mark.asyncio
async def test_own_remove_echo_landing_before_a_newer_readd_publishes_keeps_the_host(bus, database, gate):
    writer = _worker(bus)
    sibling = _worker(bus)
    await _write(writer, database, "v0")
    bus.held = True

    await _remove(writer, database)
    readd = await _paused_write(writer, database, gate, "v1")
    await bus.deliver_in_flight()
    gate.resume.set()
    await asyncio.wait_for(readd, 5)
    await bus.deliver_in_flight()

    assert database[HOST_ID] == "v1"
    assert await _cached_remark(writer) == "v1"
    assert await _cached_remark(sibling) == "v1"


@pytest.mark.asyncio
async def test_own_remove_echo_landing_after_a_newer_readd_applied_keeps_the_host(bus, database, gate):
    writer = _worker(bus)
    sibling = _worker(bus)
    await _write(writer, database, "v0")
    bus.held = True

    await _remove(writer, database)
    await _write(writer, database, "v1")
    await bus.deliver_in_flight()

    assert database[HOST_ID] == "v1"
    assert await _cached_remark(writer) == "v1"
    assert await _cached_remark(sibling) == "v1"


@pytest.mark.asyncio
async def test_sibling_remove_landing_before_an_enable_publishes_does_not_drop_the_enable(bus, database, gate):
    enabler = _worker(bus)
    remover = _worker(bus)
    await _write(enabler, database, "v0")
    bus.held = True

    await _remove(remover, database)
    enable = await _paused_write(enabler, database, gate, "v1")
    await bus.deliver_in_flight()
    gate.resume.set()
    await asyncio.wait_for(enable, 5)
    await bus.deliver_in_flight()

    assert database[HOST_ID] == "v1"
    assert await _cached_remark(enabler) == "v1"
    assert await _cached_remark(remover) == "v1"


@pytest.mark.asyncio
async def test_sibling_disable_landing_before_an_enable_publishes_does_not_drop_the_enable(bus, database, gate):
    enabler = _worker(bus)
    disabler = _worker(bus)
    await _write(enabler, database, "v0")
    bus.held = True

    await _write(disabler, database, "disabled-v0")
    enable = await _paused_write(enabler, database, gate, "v1")
    await bus.deliver_in_flight()
    gate.resume.set()
    await asyncio.wait_for(enable, 5)
    await bus.deliver_in_flight()

    assert database[HOST_ID] == "v1"
    assert await _cached_remark(enabler) == "v1"
    assert await _cached_remark(disabler) == "v1"
