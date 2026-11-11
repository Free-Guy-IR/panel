import json
from contextlib import asynccontextmanager
from http import HTTPStatus
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from PasarGuardNodeBridge import NodeAPIError

from app.models.node import NodeCoreUpdate, NodeGeoFilesUpdate
from app.node import worker as node_worker
from app.operation import OperatorType, node as node_operation_module
from app.operation.node import NodeOperation


class _Msg:
    def __init__(self):
        self.replies: list[bytes] = []

    async def respond(self, data: bytes):
        self.replies.append(data)


@asynccontextmanager
async def _no_db():
    yield None


def _failing_node(code: int):
    async def fail(*args, **kwargs):
        raise NodeAPIError(code, f"bridge said {code}")

    return SimpleNamespace(
        update_node=fail,
        update_core=fail,
        update_geofiles=fail,
        get_system_stats=fail,
        get_outbounds_latency=fail,
        get_user_online_stats=fail,
    )


def _local_operator(operator_type: OperatorType) -> NodeOperation:
    operator = NodeOperation(operator_type)
    operator._update_node_api_impl = operator._update_node_api_local
    operator._update_core_impl = operator._update_core_local
    operator._update_geofiles_impl = operator._update_geofiles_local
    operator._get_node_stats_impl = operator._get_node_system_stats_local
    operator._get_outbounds_latency_impl = operator._get_outbounds_latency_local
    operator._get_user_online_stats_impl = operator._get_user_online_stats_local

    async def validated(db, node_id):
        return None

    operator._validate_node_then_release_db = validated
    return operator


def _worker_service(monkeypatch, node) -> node_worker.NodeWorkerService:
    async def get_node(node_id):
        return node

    async def get_user_by_id(db, user_id):
        return SimpleNamespace(id=user_id)

    monkeypatch.setattr(node_operation_module, "node_manager", SimpleNamespace(get_node=get_node))
    monkeypatch.setattr(node_operation_module, "get_user_by_id", get_user_by_id)
    monkeypatch.setattr(node_worker, "GetDB", _no_db)
    service = node_worker.NodeWorkerService()
    service._node_operator = _local_operator(OperatorType.SYSTEM)
    return service


class _Loopback:
    is_closed = False

    def __init__(self, service):
        self._service = service
        self.replies: list[dict] = []

    async def request(self, subject, data, timeout=None):
        message = json.loads(data.decode())
        msg = _Msg()
        await self._service._run_rpc(msg, message["action"], message["payload"])
        self.replies.append(json.loads(msg.replies[0].decode()))
        return SimpleNamespace(data=msg.replies[0])


def _wire(monkeypatch, client) -> None:
    async def get_client():
        return client

    monkeypatch.setattr(node_operation_module.node_nats_client, "_get_client", get_client)


async def _status(call) -> int:
    with pytest.raises(HTTPException) as caught:
        await call
    return caught.value.status_code


REMOTE_CALLS = {
    "update_node_api": lambda op: op._update_node_api_remote(1),
    "update_core": lambda op: op._update_core_remote(1, NodeCoreUpdate(core_version="v1.2.3")),
    "update_geofiles": lambda op: op._update_geofiles_remote(1, NodeGeoFilesUpdate()),
    "get_node_system_stats": lambda op: op._get_node_system_stats_remote(1),
    "get_outbounds_latency": lambda op: op._get_outbounds_latency_remote(1),
    "get_user_online_stats": lambda op: op._get_user_online_stats_remote(None, 1, 7),
}

LOCAL_CALLS = {
    "update_node_api": lambda op: op._update_node_api_local(1),
    "update_core": lambda op: op._update_core_local(1, NodeCoreUpdate(core_version="v1.2.3")),
    "update_geofiles": lambda op: op._update_geofiles_local(1, NodeGeoFilesUpdate()),
    "get_node_system_stats": lambda op: op._get_node_system_stats_local(1),
    "get_outbounds_latency": lambda op: op._get_outbounds_latency_local(1),
    "get_user_online_stats": lambda op: op._get_user_online_stats_local(None, 1, 7),
}

MAINTENANCE = ["update_node_api", "update_core", "update_geofiles"]
QUERIES = ["get_node_system_stats", "get_outbounds_latency", "get_user_online_stats"]


@pytest.mark.parametrize("action", MAINTENANCE + QUERIES)
@pytest.mark.parametrize("bridge_code", [404, 403, 409, 422, 503])
async def test_a_bridge_http_error_reaches_the_split_role_caller_with_its_status(monkeypatch, action, bridge_code):
    service = _worker_service(monkeypatch, _failing_node(bridge_code))
    loopback = _Loopback(service)
    _wire(monkeypatch, loopback)

    status = await _status(REMOTE_CALLS[action](NodeOperation(OperatorType.API)))

    assert loopback.replies == [{"ok": False, "error": f"bridge said {bridge_code}", "code": bridge_code}]
    assert status == bridge_code


@pytest.mark.parametrize("action", MAINTENANCE)
@pytest.mark.parametrize(("bridge_code", "expected"), [(-1, 504), (-5, 503), (0, 503), (404, 404), (500, 500)])
async def test_split_role_maintenance_answers_what_the_single_process_panel_answers(
    monkeypatch, action, bridge_code, expected
):
    node = _failing_node(bridge_code)
    service = _worker_service(monkeypatch, node)
    _wire(monkeypatch, _Loopback(service))

    remote = await _status(REMOTE_CALLS[action](NodeOperation(OperatorType.API)))
    local = await _status(LOCAL_CALLS[action](_local_operator(OperatorType.API)))

    assert (remote, local) == (expected, expected)


@pytest.mark.parametrize("action", QUERIES)
@pytest.mark.parametrize(("bridge_code", "expected"), [(-5, 408), (-1, 408), (0, 408), (404, 404), (500, 500)])
async def test_split_role_queries_answer_what_the_single_process_panel_answers(
    monkeypatch, action, bridge_code, expected
):
    node = _failing_node(bridge_code)
    service = _worker_service(monkeypatch, node)
    _wire(monkeypatch, _Loopback(service))

    remote = await _status(REMOTE_CALLS[action](NodeOperation(OperatorType.API)))
    local = await _status(LOCAL_CALLS[action](_local_operator(OperatorType.API)))

    assert (remote, local) == (expected, expected)


def _coded(exc: Exception, code) -> Exception:
    exc.code = code
    return exc


async def _worker_reply(monkeypatch, exc: Exception) -> dict:
    async def raise_it(data):
        raise exc

    service = _worker_service(monkeypatch, None)
    service.register_rpc_handler("probe", raise_it)
    msg = _Msg()
    await service._run_rpc(msg, "probe", {})
    assert len(msg.replies) == 1
    return json.loads(msg.replies[0].decode())


@pytest.mark.parametrize("code", [400, 403, 404, 408, 409, 422, 499, 500, 503, 504, 599])
async def test_the_worker_answers_with_an_http_error_code_the_exception_carries(monkeypatch, code):
    reply = await _worker_reply(monkeypatch, _coded(ValueError("boom"), code))

    assert reply == {"ok": False, "error": "boom", "code": code}


@pytest.mark.parametrize("code", [-5, -4, -3, -2, -1, 0, 1, 200, 302, 399, 600, 601, 1000, "404", 404.0, None])
@pytest.mark.parametrize(
    ("message", "fallback"),
    [("boom", 500), ("Node not found", 404), ("NotFound: x", 404), ("not allowed", 403), ("permission denied", 403)],
)
async def test_a_code_outside_the_http_error_range_keeps_the_message_mapping(monkeypatch, code, message, fallback):
    reply = await _worker_reply(monkeypatch, _coded(ValueError(message), code))

    assert reply == {"ok": False, "error": message, "code": fallback}


@pytest.mark.parametrize(
    ("exc", "fallback"),
    [
        (RuntimeError("node_id is required"), 500),
        (RuntimeError("Node not found"), 404),
        (ValueError("permission denied"), 403),
        (KeyError("x"), 500),
    ],
)
async def test_an_exception_without_a_code_keeps_the_message_mapping(monkeypatch, exc, fallback):
    reply = await _worker_reply(monkeypatch, exc)

    assert reply == {"ok": False, "error": str(exc), "code": fallback}


@pytest.mark.parametrize(("bridge_code", "expected"), [(409, 409), (503, 503), (-5, 500), (-1, 500), (600, 500)])
async def test_a_bridge_error_escaping_to_the_worker_uses_its_code_only_in_the_http_range(
    monkeypatch, bridge_code, expected
):
    reply = await _worker_reply(monkeypatch, NodeAPIError(bridge_code, "bridge failed"))

    assert reply["code"] == expected


@pytest.mark.parametrize("operator_type", [OperatorType.SYSTEM, OperatorType.CLI, OperatorType.TELEGRAM])
@pytest.mark.parametrize(("code", "carried"), [(409, 409), (504, 504), (-5, 408), (0, 408)])
async def test_a_non_api_operator_error_carries_the_status_an_api_operator_would_raise(
    operator_type, code, carried
):
    with pytest.raises(ValueError) as caught:
        await NodeOperation(operator_type).raise_error(message="nope", code=code)

    assert str(caught.value) == "nope"
    assert caught.value.code == carried
    with pytest.raises(HTTPException) as api:
        await NodeOperation(OperatorType.API).raise_error(message="nope", code=code)
    assert api.value.status_code == carried


class _OldWorker:
    is_closed = False

    def __init__(self, reply: dict):
        self._reply = reply

    async def request(self, subject, data, timeout=None):
        return SimpleNamespace(data=json.dumps(self._reply).encode())


@pytest.mark.parametrize("action", MAINTENANCE + QUERIES)
@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ({"ok": False, "error": "bridge said 409"}, 500),
        ({"ok": False, "error": "invalid payload"}, 500),
        ({"ok": False, "error": "bridge said 409", "code": 500}, 500),
        ({"ok": False, "error": "Node not found", "code": 404}, 404),
        ({"ok": False, "error": "permission denied", "code": 403}, 403),
    ],
)
async def test_a_panel_still_reads_an_old_worker_reply(monkeypatch, action, reply, expected):
    _wire(monkeypatch, _OldWorker(reply))

    assert await _status(REMOTE_CALLS[action](NodeOperation(OperatorType.API))) == expected


@pytest.mark.parametrize("bridge_code", [404, 409, 503, -1, -5])
async def test_a_new_worker_reply_keeps_the_wire_shape_an_old_panel_reads(monkeypatch, bridge_code):
    service = _worker_service(monkeypatch, _failing_node(bridge_code))
    msg = _Msg()

    await service._run_rpc(msg, "update_node_api", {"node_id": 1})

    reply = json.loads(msg.replies[0].decode())
    assert set(reply) == {"ok", "error", "code"}
    assert reply["ok"] is False
    assert reply["error"] == f"bridge said {bridge_code}"
    assert type(reply["code"]) is int
    assert 400 <= reply["code"] <= 599
    old_panel_code = reply.get("code", 500)
    assert old_panel_code == {-1: 504, -5: 503}.get(bridge_code, bridge_code)


@pytest.mark.parametrize("action", MAINTENANCE + QUERIES)
async def test_a_node_401_reaches_the_single_process_caller_as_502(monkeypatch, action):
    _worker_service(monkeypatch, _failing_node(401))

    assert await _status(LOCAL_CALLS[action](_local_operator(OperatorType.API))) == 502


@pytest.mark.parametrize("action", MAINTENANCE + QUERIES)
async def test_a_node_401_reaches_the_split_role_caller_as_502(monkeypatch, action):
    service = _worker_service(monkeypatch, _failing_node(401))
    loopback = _Loopback(service)
    _wire(monkeypatch, loopback)

    status = await _status(REMOTE_CALLS[action](NodeOperation(OperatorType.API)))

    assert status == 502
    assert loopback.replies[0]["code"] == 502


@pytest.mark.parametrize("action", MAINTENANCE + QUERIES)
async def test_a_worker_reply_carrying_401_reaches_the_panel_caller_as_502(monkeypatch, action):
    _wire(monkeypatch, _OldWorker({"ok": False, "error": "invalid api key", "code": 401}))

    assert await _status(REMOTE_CALLS[action](NodeOperation(OperatorType.API))) == 502


async def test_a_bridge_401_escaping_to_the_worker_is_answered_as_502(monkeypatch):
    reply = await _worker_reply(monkeypatch, NodeAPIError(401, "invalid api key"))

    assert reply["code"] == 502


@pytest.mark.parametrize("operator_type", [OperatorType.API, OperatorType.WEB])
async def test_the_panels_own_401_stays_401(operator_type):
    call = NodeOperation(operator_type).raise_error(message="Could not validate credentials", code=401)

    assert await _status(call) == 401


@pytest.mark.parametrize(
    ("code", "expected"),
    [(HTTPStatus.CONFLICT, 409), (HTTPStatus.SERVICE_UNAVAILABLE, 503), (HTTPStatus.UNAUTHORIZED, 502)],
)
async def test_the_worker_answers_with_an_int_enum_code_as_a_plain_int(monkeypatch, code, expected):
    reply = await _worker_reply(monkeypatch, _coded(ValueError("boom"), code))

    assert reply == {"ok": False, "error": "boom", "code": expected}
    assert type(reply["code"]) is int


@pytest.mark.parametrize("code", [True, False])
async def test_the_worker_rejects_a_bool_code(monkeypatch, code):
    reply = await _worker_reply(monkeypatch, _coded(ValueError("boom"), code))

    assert reply == {"ok": False, "error": "boom", "code": 500}
