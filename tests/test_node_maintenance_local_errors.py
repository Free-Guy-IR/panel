import asyncio
import contextlib
import socket
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from fastapi import HTTPException
from PasarGuardNodeBridge import NodeAPIError
from PasarGuardNodeBridge.controller import Controller

from app.models.node import NodeCoreUpdate, NodeGeoFilesUpdate
from app.operation import OperatorType, node as node_operation_module
from app.operation.node import NodeOperation

BRIDGE_ERRORS = [
    pytest.param(-1, 504, id="bridge-timeout"),
    pytest.param(-2, 503, id="bridge-connection-error"),
    pytest.param(-3, 503, id="bridge-bad-version"),
    pytest.param(-4, 503, id="bridge-invalid-node"),
    pytest.param(-5, 503, id="bridge-request-error"),
    pytest.param(0, 503, id="bridge-unknown"),
    pytest.param(404, 404, id="node-404"),
    pytest.param(409, 409, id="node-409"),
    pytest.param(500, 500, id="node-500"),
]

CALLS = {
    "update_node": lambda op: op._update_node_api_local(1),
    "update_core": lambda op: op._update_core_local(1, NodeCoreUpdate(core_version="v1.2.3")),
    "update_geofiles": lambda op: op._update_geofiles_local(1, NodeGeoFilesUpdate()),
}


def _failing_node(code: int):
    async def fail(*args, **kwargs):
        raise NodeAPIError(code, f"bridge said {code}")

    return SimpleNamespace(update_node=fail, update_core=fail, update_geofiles=fail)


@pytest.mark.parametrize("action", list(CALLS))
@pytest.mark.parametrize(("bridge_code", "status"), BRIDGE_ERRORS)
async def test_local_maintenance_bridge_errors_map_to_http_status(monkeypatch, action, bridge_code, status):
    node = _failing_node(bridge_code)

    async def get_node(node_id):
        return node

    monkeypatch.setattr(node_operation_module, "node_manager", SimpleNamespace(get_node=get_node))
    operation = NodeOperation(OperatorType.API)

    with pytest.raises(HTTPException) as caught:
        await CALLS[action](operation)

    assert caught.value.status_code == status
    assert caught.value.detail == f"bridge said {bridge_code}"


@contextlib.asynccontextmanager
async def _hanging_node_service():
    release = asyncio.Event()

    async def hang(request):
        await release.wait()
        return web.Response(text="late")

    app = web.Application()
    app.router.add_post("/node/update", hang)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        release.set()
        await runner.cleanup()


def _closed_port_url():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


async def _status_through_real_bridge_request(monkeypatch, base_url):
    async with aiohttp.ClientSession(base_url=base_url) as session:
        controller = SimpleNamespace(_json_client=session, _default_timeout=10)

        async def update_node():
            return await Controller._make_json_request(controller, "POST", "/node/update", timeout=0.3)

        node = SimpleNamespace(update_node=update_node)

        async def get_node(node_id):
            return node

        monkeypatch.setattr(node_operation_module, "node_manager", SimpleNamespace(get_node=get_node))
        with pytest.raises(HTTPException) as caught:
            await NodeOperation(OperatorType.API)._update_node_api_local(1)
    return caught.value.status_code


async def test_a_real_bridge_request_timeout_answers_504(monkeypatch):
    async with _hanging_node_service() as base_url:
        assert await _status_through_real_bridge_request(monkeypatch, base_url) == 504


async def test_a_real_bridge_connection_refused_answers_503(monkeypatch):
    assert await _status_through_real_bridge_request(monkeypatch, _closed_port_url()) == 503
