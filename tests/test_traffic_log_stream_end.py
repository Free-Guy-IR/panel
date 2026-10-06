import asyncio
import importlib
from uuid import uuid4

import certifi
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler
from grpclib.server import Server
from PasarGuardNodeBridge.common import service_grpc, service_pb2 as service

from app.fork.traffic_log.collector import STATE_ERROR, TrafficCollector
from app.node.bridge import QueuedGrpcNode


class EndingLogNode:
    def __init__(self):
        self.opened = asyncio.Event()

    async def logs(self, stream):
        await stream.recv_message()
        self.opened.set()
        await stream.send_message(service.Log(detail="a line the collector cannot parse"))

    async def backend_stats(self, stream):
        await stream.recv_message()
        await stream.send_message(service.BackendStatsResponse())

    def __mapping__(self):
        return {
            "/service.NodeService/GetLogs": Handler(self.logs, Cardinality.UNARY_STREAM, service.Empty, service.Log),
            "/service.NodeService/GetBackendStats": Handler(
                self.backend_stats, Cardinality.UNARY_UNARY, service.Empty, service.BackendStatsResponse
            ),
        }


async def test_a_log_stream_the_node_ends_detaches_the_collector_instead_of_hanging(monkeypatch):
    fake = EndingLogNode()
    server = Server([fake])
    await server.start("127.0.0.1", 0)
    port = server._server.sockets[0].getsockname()[1]
    node = QueuedGrpcNode(
        address="127.0.0.1",
        port=port,
        api_port=1,
        server_ca=certifi.contents(),
        api_key=str(uuid4()),
        node_id="traffic-log-end",
    )
    node.channel.close()
    node.channel = Channel("127.0.0.1", port, ssl=False)
    node._client = service_grpc.NodeServiceStub(node.channel)
    monkeypatch.setattr(importlib.import_module("app.fork.traffic_log.collector"), "IDLE_PROBE_SECONDS", 60.0)
    traffic = TrafficCollector()
    traffic.available = True
    traffic.enabled = True
    traffic._started = True
    try:
        await node.connect("0.6.22", "interop")
        await traffic._attach(7, node, "ending")
        reader = traffic._readers[7]
        await asyncio.wait_for(fake.opened.wait(), 5)
        await asyncio.wait_for(asyncio.shield(reader), 5)
        state = traffic._states[7]
        assert state.state == STATE_ERROR, state.snapshot()
        assert "log stream ended" in str(state.snapshot()).lower(), state.snapshot()
        assert 7 not in traffic._readers
    finally:
        reader = traffic._readers.pop(7, None)
        if reader is not None:
            reader.cancel()
        await node.disconnect()
        node.channel.close()
        await node._json_client.close()
        server.close()
        await server.wait_closed()
