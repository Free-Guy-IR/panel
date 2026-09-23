import asyncio
import json
from types import SimpleNamespace

import nats.errors as nats_errors
import pytest

from app import settings as settings_module
from app.core import hosts as hosts_module, manager as manager_module
from app.core.hosts import HostManager
from app.core.manager import CoreManager
from app.db.models import CoreType
from app.models.host import BaseHost
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.nats import client as client_module, kv_cas, router as router_module
from app.nats.kv_cas import MemoryCasKv, kv_cas_json
from app.utils.crypto import generate_wireguard_keypair


def _inbound(remark: str, priority: int = 1) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark=remark,
        inbound_tag="in",
        protocol="vless",
        network="tcp",
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(),
        priority=priority,
    )


class _Entry:
    def __init__(self, value: bytes):
        self.value = value
        self.revision = 1


class _SnapshotKV:
    def __init__(self, state: dict):
        self.stored = json.dumps(state).encode()

    async def get(self, key):
        return _Entry(self.stored)

    async def put(self, key, value):
        self.stored = value


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_nats_clients_are_told_to_reconnect_forever(monkeypatch: pytest.MonkeyPatch):
    captured = {}

    class _Connected:
        is_closed = False

    async def connect(url, **options):
        captured.update(options)
        return _Connected()

    monkeypatch.setattr(client_module, "is_nats_enabled", lambda: True)
    monkeypatch.setattr(client_module.nats, "connect", connect)

    await client_module.create_nats_client()

    assert captured["max_reconnect_attempts"] == -1
    for callback in ("error_cb", "disconnected_cb", "reconnected_cb", "closed_cb"):
        assert callable(captured[callback])


@pytest.mark.asyncio
async def test_a_reconnect_notifies_listeners_with_the_client_that_reconnected():
    seen = []
    client = object()

    async def listener(reconnected):
        seen.append(reconnected)

    callbacks = client_module._connection_callbacks("nats://example", {"client": client})
    client_module.add_reconnect_listener(listener)
    try:
        await callbacks["error_cb"](ConnectionError("down"))
        await callbacks["disconnected_cb"]()
        await callbacks["reconnected_cb"]()
    finally:
        client_module.remove_reconnect_listener(listener)

    assert seen == [client]


class _LiveClient:
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.is_closed = False

    async def subscribe(self, subject):
        queue = self.queue

        class _Subscription:
            @property
            def messages(self):
                async def iterate():
                    while True:
                        yield await queue.get()

                return iterate()

        return _Subscription()

    async def publish(self, subject, payload):
        return None

    async def close(self):
        self.is_closed = True


@pytest.mark.asyncio
async def test_the_router_resyncs_after_subscribing_and_after_its_own_client_reconnects(
    monkeypatch: pytest.MonkeyPatch,
):
    live = _LiveClient()

    async def create_client():
        return live

    monkeypatch.setattr(router_module, "_router_enabled", lambda: True)
    monkeypatch.setattr(router_module, "create_nats_client", create_client)
    router = router_module.NatsMessageRouter()
    resyncs = []

    async def resync():
        resyncs.append(len(resyncs))

    router.register_resync_handler(resync)
    await router.start()
    try:
        for _ in range(50):
            if resyncs:
                break
            await asyncio.sleep(0.02)
        assert len(resyncs) == 1

        for listener in tuple(client_module._reconnect_listeners):
            await listener(object())
        await asyncio.sleep(0.05)
        assert len(resyncs) == 1

        for listener in tuple(client_module._reconnect_listeners):
            await listener(live)
        for _ in range(50):
            if len(resyncs) == 2:
                break
            await asyncio.sleep(0.02)
    finally:
        await router.stop()

    assert len(resyncs) == 2
    assert router._on_reconnected not in client_module._reconnect_listeners


@pytest.mark.asyncio
async def test_the_router_also_resyncs_periodically_so_a_single_lost_publish_cannot_last_forever(
    monkeypatch: pytest.MonkeyPatch,
):
    live = _LiveClient()

    async def create_client():
        return live

    monkeypatch.setattr(router_module, "_router_enabled", lambda: True)
    monkeypatch.setattr(router_module, "create_nats_client", create_client)
    assert 0 < router_module.PERIODIC_RESYNC_S <= 600
    monkeypatch.setattr(router_module, "PERIODIC_RESYNC_S", 0.05)
    router = router_module.NatsMessageRouter()
    resyncs = []

    async def resync():
        resyncs.append(True)

    router.register_resync_handler(resync)
    await router.start()
    try:
        for _ in range(100):
            if len(resyncs) >= 3:
                break
            await asyncio.sleep(0.02)
    finally:
        await router.stop()

    assert len(resyncs) >= 3


@pytest.mark.asyncio
async def test_settings_edited_by_another_worker_are_seen_once_the_cache_entry_expires(
    monkeypatch: pytest.MonkeyPatch,
):
    stored = {"general": {"default_flow": ""}}

    async def stored_settings():
        return SimpleNamespace(general=dict(stored["general"]))

    monkeypatch.setattr(settings_module, "stored_settings", stored_settings)
    _plain_settings_models(monkeypatch)
    cache = settings_module.general_settings.cache
    await cache.clear()
    loop = asyncio.get_running_loop()
    try:
        first = await settings_module.general_settings()
        stored["general"]["default_flow"] = "xtls-rprx-vision"
        cached = await settings_module.general_settings()
        expiries = list(cache._handlers.values())
        assert len(expiries) == 1, "the settings cache entry has no expiry, so a missed invalidation lasts forever"
        remaining = expiries[0].when() - loop.time()
        assert 0 < remaining <= settings_module.SETTINGS_CACHE_TTL_S
        expiries[0]._run()
        expiries[0].cancel()
        refreshed = await settings_module.general_settings()
    finally:
        await cache.clear()

    assert first.default_flow == cached.default_flow == ""
    assert refreshed.default_flow == "xtls-rprx-vision"


@pytest.mark.asyncio
async def test_every_settings_cache_entry_expires(monkeypatch: pytest.MonkeyPatch):
    row = SimpleNamespace(
        telegram={},
        webhook={},
        notification_settings={},
        notification_enable={},
        subscription={},
        hwid={},
        general={},
    )

    async def stored_settings():
        return row

    monkeypatch.setattr(settings_module, "stored_settings", stored_settings)
    _plain_settings_models(monkeypatch)
    getters = [
        settings_module.telegram_settings,
        settings_module.webhook_settings,
        settings_module.notification_settings,
        settings_module.notification_enable,
        settings_module.subscription_settings,
        settings_module.hwid_settings,
        settings_module.general_settings,
    ]
    await settings_module.refresh_caches()
    try:
        for getter in getters:
            await getter()
            assert len(getter.cache._handlers) == 1, f"{getter.__name__} is cached with no expiry"
    finally:
        await settings_module.refresh_caches()


class _PlainModel:
    @staticmethod
    def model_validate(value):
        return SimpleNamespace(**value)


def _plain_settings_models(monkeypatch: pytest.MonkeyPatch) -> None:
    names = (
        "Telegram",
        "Webhook",
        "NotificationSettings",
        "NotificationEnable",
        "Subscription",
        "HWIDSettings",
        "General",
    )
    monkeypatch.setattr(settings_module, "settings", SimpleNamespace(**dict.fromkeys(names, _PlainModel)))


def _wg_row(core_id: int, listen_port: int, private_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=core_id,
        name=f"wg{core_id}",
        type=CoreType.wg,
        config={
            "interface_name": f"wg{core_id}",
            "private_key": private_key,
            "listen_port": listen_port,
            "address": [f"10.{core_id}.0.1/24"],
        },
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )


@pytest.mark.asyncio
async def test_a_resync_from_the_database_never_overwrites_a_newer_update_applied_meanwhile(
    monkeypatch: pytest.MonkeyPatch,
):
    private_key, _ = generate_wireguard_keypair()
    old_row = _wg_row(1, 51820, private_key)
    new_row = _wg_row(1, 51999, private_key)
    database = [old_row]
    first_read_started = asyncio.Event()
    release_first_read = asyncio.Event()
    reads = []

    async def get_core_configs(db, query):
        reads.append(list(database))
        snapshot = list(database)
        if len(reads) == 1:
            first_read_started.set()
            await release_first_read.wait()
        return snapshot, len(snapshot)

    monkeypatch.setattr(manager_module, "get_core_configs", get_core_configs)
    monkeypatch.setattr(manager_module, "GetDB", _Session)
    worker = CoreManager()
    worker._nats_enabled = True
    worker._kv = None

    resync = asyncio.create_task(worker._reload_from_cache())
    try:
        await asyncio.wait_for(first_read_started.wait(), timeout=2.0)
    except TimeoutError:
        resync.cancel()
        pytest.fail("a resync never read the database; it reloaded the persistent KV snapshot instead")
    database[:] = [new_row]
    await worker._update_core_local(new_row)
    release_first_read.set()
    await resync

    loaded = (await worker.get_cores())[1]
    assert loaded.to_json()["config"]["listen_port"] == 51999
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_a_restart_loads_hosts_from_the_database_not_an_older_kv_snapshot(monkeypatch: pytest.MonkeyPatch):
    db_reads = []

    async def get_hosts(db, query=None):
        db_reads.append(True)
        return [BaseHost(id=7, remark="from-database", priority=1, inbound_tag="in")]

    async def prepare(db, host, inbounds_list):
        return host.id, _inbound(host.remark, host.priority)

    async def upsert_inbounds(db, inbounds):
        return None

    async def no_persist():
        return None

    monkeypatch.setattr(hosts_module, "get_hosts", get_hosts)
    monkeypatch.setattr(hosts_module, "upsert_inbounds", upsert_inbounds)
    monkeypatch.setattr(HostManager, "_prepare_host_entry", staticmethod(prepare))
    restarted = HostManager()
    restarted._nats_enabled = False
    restarted._kv = _SnapshotKV({"7": _inbound("from-old-kv-snapshot").model_dump()})
    monkeypatch.setattr(restarted, "_persist_state", no_persist)

    await restarted.setup(_Session())

    host = await restarted.get_host(7)
    assert db_reads, "the restarted worker never read the hosts table and served the persistent KV snapshot"
    assert host is not None and host["remark"] == "from-database"


@pytest.mark.asyncio
async def test_a_host_resync_drops_hosts_deleted_while_messages_were_missed(monkeypatch: pytest.MonkeyPatch):
    database = [BaseHost(id=1, remark="kept", priority=1, inbound_tag="in")]

    async def get_hosts(db, query=None):
        return list(database)

    async def prepare(db, host, inbounds_list):
        return host.id, _inbound(host.remark, host.priority)

    async def no_persist():
        return None

    monkeypatch.setattr(hosts_module, "get_hosts", get_hosts)
    monkeypatch.setattr(hosts_module, "GetDB", _Session)
    monkeypatch.setattr(HostManager, "_prepare_host_entry", staticmethod(prepare))
    worker = HostManager()
    worker._nats_enabled = True
    monkeypatch.setattr(worker, "_persist_state", no_persist)
    await worker._add_prepared_hosts_local([(1, _inbound("kept")), (2, _inbound("deleted-elsewhere"))])

    await worker._reload_from_cache()

    assert await worker.get_host(2) is None
    assert (await worker.get_host(1))["remark"] == "kept"


class _LandsThenTimesOut(MemoryCasKv):
    async def update(self, key, value, last=None):
        await super().update(key, value, last=last)
        raise nats_errors.TimeoutError

    async def create(self, key, value):
        await super().create(key, value)
        raise nats_errors.TimeoutError


class _TimesOutWithoutWriting(MemoryCasKv):
    async def create(self, key, value):
        raise nats_errors.TimeoutError


@pytest.mark.asyncio
async def test_a_cas_write_that_landed_before_its_reply_timed_out_counts_as_won():
    kv = _LandsThenTimesOut()
    assert await kv_cas_json(kv, "lease", {"token": "mine"}, 0) is True
    _, revision = await kv_cas.kv_get_json(kv, "lease")
    assert await kv_cas_json(kv, "lease", {"token": "mine", "renewed": True}, revision) is True


@pytest.mark.asyncio
async def test_a_cas_write_that_never_landed_or_lost_the_race_counts_as_lost():
    assert await kv_cas_json(_TimesOutWithoutWriting(), "lease", {"token": "mine"}, 0) is False
    kv = MemoryCasKv()
    await kv.create("lease", b'{"token":"theirs"}')
    assert await kv_cas_json(kv, "lease", {"token": "mine"}, 0) is False
