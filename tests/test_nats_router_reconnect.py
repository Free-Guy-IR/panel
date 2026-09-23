import asyncio
import json

import pytest

from app.nats import router as router_module
from app.nats.message import MessageTopic, NatsMessage


class _Msg:
    def __init__(self, payload: dict):
        self.data = json.dumps(payload).encode()


class _Subscription:
    def __init__(self, queue: asyncio.Queue | None):
        self._queue = queue

    @property
    def messages(self):
        return self._iterate()

    async def _iterate(self):
        if self._queue is None:
            return
        while True:
            yield await self._queue.get()


class _Client:
    def __init__(self, queue: asyncio.Queue | None):
        self.queue = queue
        self.is_closed = queue is None

    async def subscribe(self, subject):
        return _Subscription(self.queue)

    async def publish(self, subject, payload):
        if self.is_closed:
            raise ConnectionError("nats: connection closed")
        await self.queue.put(_Msg(json.loads(payload)))

    async def close(self):
        self.is_closed = True


@pytest.mark.asyncio
async def test_the_router_keeps_delivering_after_its_nats_connection_was_closed(monkeypatch):
    live_queue: asyncio.Queue = asyncio.Queue()
    clients = [_Client(None), _Client(live_queue)]
    created = []

    async def create_client():
        client = clients[min(len(created), len(clients) - 1)]
        created.append(client)
        return client

    monkeypatch.setattr(router_module, "_router_enabled", lambda: True)
    monkeypatch.setattr(router_module, "create_nats_client", create_client)

    router = router_module.NatsMessageRouter()
    refreshed = asyncio.Event()

    async def on_settings(_):
        refreshed.set()

    router.register_handler(MessageTopic.SETTING, on_settings)
    await router.start()
    await asyncio.sleep(0.2)

    await live_queue.put(
        _Msg(json.loads(NatsMessage(topic=MessageTopic.SETTING, data={"action": "refresh"}).model_dump_json()))
    )
    try:
        await asyncio.wait_for(refreshed.wait(), timeout=3)
    except TimeoutError:
        pass
    finally:
        await router.stop()

    assert refreshed.is_set(), (
        f"the router's subscription ended when its NATS connection closed and it never subscribed again "
        f"(clients created: {len(created)}, running={router._running}); every settings/host/core/template "
        "invalidation published afterwards is ignored by this worker until it restarts"
    )
