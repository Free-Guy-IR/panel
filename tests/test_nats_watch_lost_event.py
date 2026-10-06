import asyncio
import contextlib

import nats

from app.node.nats_memory import NatsUserSyncStore
from tests.test_nats_sync_integration import jetstream, wait_for_keys  # noqa: F401


class DroppingProxy:
    def __init__(self, port):
        self.port = port
        self.dropping = False
        self.client_writers = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._link, "127.0.0.1", 0)
        return f"nats://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"

    async def _link(self, client_reader, client_writer):
        server_reader, server_writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.client_writers.append(client_writer)

        async def pump(reader, writer, from_server):
            with contextlib.suppress(Exception):
                while data := await reader.read(65536):
                    if from_server and self.dropping:
                        continue
                    writer.write(data)
                    await writer.drain()
            writer.close()

        await asyncio.gather(
            pump(client_reader, server_writer, False),
            pump(server_reader, client_writer, True),
            return_exceptions=True,
        )

    def cut_clients(self):
        for writer in self.client_writers:
            writer.transport.abort()
        self.client_writers.clear()
        self.dropping = False

    async def close(self):
        self.cut_clients()
        self.server.close()


async def test_a_live_update_lost_on_a_dead_connection_still_reaches_the_index(jetstream):  # noqa: F811
    port = int(jetstream.url.rsplit(":", 1)[1])
    proxy = DroppingProxy(port)
    nc = await nats.connect(await proxy.start(), reconnect_time_wait=0.05)
    store = NatsUserSyncStore(await nc.jetstream().key_value(jetstream.bucket))
    try:
        assert await store.claim_users("1", "reader", 10, 30) == []
        proxy.dropping = True
        await jetstream.kv.put("p.1.lost", b'{"email":"lost","user":"CgRsb3N0"}')
        await asyncio.sleep(0.2)
        proxy.cut_clients()
        async with asyncio.timeout(10):
            while nc.stats["reconnects"] == 0:
                await asyncio.sleep(0.02)
        await wait_for_keys(store, "p.1.", 1, timeout=20)
    finally:
        await store.close()
        with contextlib.suppress(Exception):
            await nc.close()
        await proxy.close()
