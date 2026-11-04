import asyncio
import gc
import logging
import ssl
import traceback
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.validators import ProxyValidator

SECRET = "pw" + "Leak" + "Marker" + str(7)
USER = "u" + "x"
HOST = "proxy.example"
WIDE = ["一", "∕", "́", "Ā", "\U0001f600"]
ENCODED_WIDE = ["%E4%B8%80", "%e2%88%95", "%CC%81", "%C4%80", "%F0%9F%98%80", "%E9", "%ff"]


def _wide_urls() -> list[str]:
    urls = []
    for char in WIDE + ENCODED_WIDE:
        urls.append(f"http://{USER}:{SECRET}{char}@{HOST}:8080")
        urls.append(f"socks5://{USER}{char}:{SECRET}@{HOST}:1080")
    return urls


WIDE_URLS = _wide_urls()
LATIN1_URLS = [
    f"http://{USER}:{SECRET}é@{HOST}:8080",
    f"socks5://{USER}ÿ:{SECRET}@{HOST}:1080",
    f"http://{USER}:{SECRET}%41@{HOST}:8080",
    f"http://{USER}:{SECRET}%C3%A9@{HOST}:8080",
]


def _chain_text(error: BaseException | None) -> str:
    if error is None:
        return ""
    parts = []
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        parts.append(repr(current))
        parts.append(repr(current.args))
        current = current.__cause__ or current.__context__
    parts.append("".join(traceback.format_exception(error)))
    return "\n".join(parts)


def _assert_clean_invalid_proxy(error: BaseException) -> None:
    from app.utils.http_client import INVALID_PROXY_MESSAGE, InvalidProxyURL

    assert type(error) is InvalidProxyURL
    assert str(error) == INVALID_PROXY_MESSAGE
    assert error.args == (INVALID_PROXY_MESSAGE,)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert SECRET not in _chain_text(error)


def _node_row(proxy_url):
    from app.db.models import Node

    node = Node(
        name="n1", address="10.0.0.1", port=1000, api_port=1001, server_ca="ca", api_key="key", core_config_id=None
    )
    node.id = 7
    node.proxy_url = proxy_url
    return node


@pytest.mark.parametrize("url", WIDE_URLS)
def test_r1_validator_rejects_userinfo_aiohttp_cannot_encode(url):
    with pytest.raises(ValueError) as caught:
        ProxyValidator.validate_proxy_url(url)
    assert SECRET not in _chain_text(caught.value)


@pytest.mark.parametrize("url", LATIN1_URLS)
def test_r1_validator_keeps_latin1_userinfo(url):
    assert ProxyValidator.validate_proxy_url(url) == url


@pytest.mark.parametrize("url", LATIN1_URLS)
def test_r1_accepted_userinfo_encodes_in_every_proxy_client(url):
    import aiohttp
    import yarl
    from python_socks._helpers import parse_proxy_url

    parsed = yarl.URL(url)
    aiohttp.BasicAuth(parsed.user or "", parsed.password or "").encode()
    _, _, _, username, password = parse_proxy_url(url)
    (username or "").encode("latin-1")
    (password or "").encode("latin-1")


@pytest.mark.parametrize("url", WIDE_URLS)
def test_r1_checked_proxy_url_refuses_stored_wide_userinfo(url):
    from app.utils.http_client import checked_proxy_url

    with pytest.raises(Exception) as caught:
        checked_proxy_url(url)
    _assert_clean_invalid_proxy(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", WIDE_URLS)
async def test_r1_outbound_session_refuses_stored_wide_userinfo(url):
    from app.utils.http_client import create_outbound_http_session

    with pytest.raises(Exception) as caught:
        create_outbound_http_session(proxy=url)
    _assert_clean_invalid_proxy(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", WIDE_URLS)
async def test_r1_telegram_session_refuses_stored_wide_userinfo(url):
    from app.telegram.session import NativeTLSAiohttpSession

    with pytest.raises(Exception) as caught:
        NativeTLSAiohttpSession(proxy=url)
    _assert_clean_invalid_proxy(caught.value)


@pytest.mark.parametrize("url", WIDE_URLS)
def test_r1_node_kwargs_refuse_stored_wide_userinfo(url):
    from app.node import NodeManager

    with pytest.raises(Exception) as caught:
        NodeManager()._create_node_kwargs(_node_row(url))
    _assert_clean_invalid_proxy(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", WIDE_URLS)
async def test_r1_telegram_start_reports_fixed_message_for_wide_userinfo(url, caplog):
    from app.models.settings import RunMethod
    from app.telegram import TelegramBotManager
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    caplog.set_level(logging.DEBUG)
    manager = TelegramBotManager()
    settings = SimpleNamespace(
        method=RunMethod.WEBHOOK, proxy_url=url, token="0:" + "t" * 8, webhook_url=f"https://{HOST}"
    )
    await manager._start_locked(settings)
    assert manager._bot is None
    assert any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)
    assert all(SECRET not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_r3_rejected_proxy_builds_no_connector(monkeypatch):
    from app.utils import http_client

    built = MagicMock(side_effect=AssertionError("connector built"))
    monkeypatch.setattr(http_client.aiohttp, "TCPConnector", built)
    with pytest.raises(http_client.InvalidProxyURL):
        http_client.create_outbound_http_session(proxy=f"http://{USER}:{SECRET}#@{HOST}:8080")
    built.assert_not_called()


@pytest.mark.asyncio
async def test_r3_rejected_proxy_leaves_no_unclosed_connector():
    from app.utils import http_client

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(3):
            with pytest.raises(http_client.InvalidProxyURL):
                http_client.create_outbound_http_session(proxy=f"http://{USER}:{SECRET}#@{HOST}:8080")
        gc.collect()
        await asyncio.sleep(0)
    assert not [w for w in caught if "Unclosed" in str(w.message)]


@pytest.mark.asyncio
async def test_r3_valid_proxy_still_builds_session():
    from app.utils.http_client import create_outbound_http_session

    session = create_outbound_http_session(proxy=f"http://{USER}:{SECRET}@{HOST}:8080")
    try:
        assert session.connector is not None
    finally:
        await session.close()


def _node_operation():
    from app.operation import OperatorType
    from app.operation.node import NodeOperation

    return NodeOperation(operator_type=OperatorType.API)


def _db():
    db = MagicMock()
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    return db


ADMIN = SimpleNamespace(username="a" + "dmin")
BAD_STORED = f"http://{USER}:{SECRET}一@{HOST}:8080"


@pytest.mark.asyncio
async def test_r2_status_only_disable_works_on_a_node_with_bad_stored_proxy(monkeypatch):
    import app.operation.node as op_module
    from app.db.models import NodeStatus
    from app.models.node import NodeModify

    stored = _node_row(BAD_STORED)
    stored.status = NodeStatus.connected
    operation = _node_operation()
    operation.get_validated_node = AsyncMock(return_value=stored)

    async def crud_modify(db, db_node, modified):
        db_node.status = modified.status
        return db_node

    monkeypatch.setattr(op_module, "modify_node", crud_modify)
    disconnect = AsyncMock()
    operation.disconnect_single_node = disconnect
    update_impl = AsyncMock()
    operation._update_node_impl = update_impl
    monkeypatch.setattr(op_module, "NodeResponse", MagicMock())
    monkeypatch.setattr(op_module.notification, "modify_node", AsyncMock())
    await operation.modify_node(_db(), 7, NodeModify(status=NodeStatus.disabled), ADMIN)
    assert stored.status == NodeStatus.disabled
    disconnect.assert_awaited_once_with(7)
    update_impl.assert_not_awaited()


@pytest.mark.asyncio
async def test_r2_rename_of_a_connected_node_with_bad_stored_proxy_is_not_refused(monkeypatch):
    import app.operation.node as op_module
    from app.db.models import NodeStatus
    from app.models.node import NodeModify
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    stored = _node_row(BAD_STORED)
    stored.status = NodeStatus.connected
    operation = _node_operation()
    operation.get_validated_node = AsyncMock(return_value=stored)
    monkeypatch.setattr(op_module, "modify_node", AsyncMock(return_value=stored))
    status_update = AsyncMock()
    operation._update_single_node_status = status_update
    monkeypatch.setattr(operation, "_connect_single_node_background", AsyncMock())
    monkeypatch.setattr(op_module, "NodeResponse", MagicMock())
    monkeypatch.setattr(op_module.notification, "modify_node", AsyncMock())
    await operation.modify_node(_db(), 7, NodeModify(name="n2"), ADMIN)
    status_update.assert_awaited_once()
    args, kwargs = status_update.await_args
    assert args[2] == NodeStatus.error
    assert kwargs["message"] == INVALID_PROXY_MESSAGE


@pytest.mark.parametrize("scheme", ["https"])
def test_telegram_session_refuses_a_scheme_its_proxy_client_cannot_use(scheme):
    from app.telegram.session import NativeTLSAiohttpSession

    with pytest.raises(Exception) as caught:
        NativeTLSAiohttpSession(proxy=f"{scheme}://{USER}:{SECRET}@{HOST}:443")
    _assert_clean_invalid_proxy(caught.value)


@pytest.mark.asyncio
async def test_telegram_start_with_https_proxy_logs_fixed_message_and_stays_off(caplog):
    from app.models.settings import RunMethod
    from app.telegram import TelegramBotManager
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    caplog.set_level(logging.DEBUG)
    manager = TelegramBotManager()
    settings = SimpleNamespace(
        method=RunMethod.WEBHOOK,
        proxy_url=f"https://{USER}:{SECRET}@{HOST}:443",
        token="0:" + "t" * 8,
        webhook_url=f"https://{HOST}",
    )
    await manager._start_locked(settings)
    assert manager._bot is None
    assert any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)
    assert all(SECRET not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_proxy", [None, f"socks5://{USER}:{SECRET}@{HOST}:1080"])
async def test_r2_modify_replacing_bad_stored_proxy_is_allowed(monkeypatch, new_proxy):
    import app.operation.node as op_module
    from app.models.node import NodeModify

    stored = _node_row(BAD_STORED)
    operation = _node_operation()
    operation.get_validated_node = AsyncMock(return_value=stored)

    async def apply(db, db_node, modified):
        db_node.proxy_url = modified.proxy_url
        return db_node

    monkeypatch.setattr(op_module, "modify_node", AsyncMock(side_effect=apply))
    operation._update_node_impl = AsyncMock()
    monkeypatch.setattr(operation, "_connect_single_node_background", AsyncMock())
    monkeypatch.setattr(op_module, "NodeResponse", MagicMock())
    monkeypatch.setattr(op_module.notification, "modify_node", AsyncMock())
    await operation.modify_node(_db(), 7, NodeModify(proxy_url=new_proxy), ADMIN)
    assert stored.proxy_url == new_proxy
    operation._update_node_impl.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["create", "modify"])
async def test_r2_node_manager_refusal_after_commit_is_not_a_500(monkeypatch, path):
    import app.operation.node as op_module
    from app.db.models import NodeStatus
    from app.models.node import NodeModify
    from app.utils.http_client import INVALID_PROXY_MESSAGE, InvalidProxyURL

    stored = _node_row(f"socks5://{USER}:{SECRET}@{HOST}:1080")
    stored.status = NodeStatus.connected
    operation = _node_operation()
    operation.get_validated_node = AsyncMock(return_value=stored)
    operation.get_validated_core_config = AsyncMock()
    operation._validate_additional_cores = AsyncMock()
    monkeypatch.setattr(op_module, "create_node", AsyncMock(return_value=stored))
    monkeypatch.setattr(op_module, "modify_node", AsyncMock(return_value=stored))
    operation._update_node_impl = AsyncMock(side_effect=InvalidProxyURL(INVALID_PROXY_MESSAGE))
    status_update = AsyncMock()
    operation._update_single_node_status = status_update
    monkeypatch.setattr(operation, "_connect_single_node_background", AsyncMock())
    monkeypatch.setattr(op_module, "NodeResponse", MagicMock())
    monkeypatch.setattr(op_module.notification, "modify_node", AsyncMock())
    monkeypatch.setattr(op_module.notification, "create_node", AsyncMock())
    db = _db()
    if path == "create":
        new_node = MagicMock(core_config_id=None, additional_core_config_ids=[])
        await operation.create_node(db, new_node, ADMIN)
    else:
        await operation.modify_node(db, 7, NodeModify(name="n2"), ADMIN)
    status_update.assert_awaited_once()
    args, kwargs = status_update.await_args
    assert args[1] == 7
    assert args[2] == NodeStatus.error
    assert kwargs["message"] == INVALID_PROXY_MESSAGE
    assert SECRET not in kwargs["message"]


@pytest.mark.asyncio
async def test_r4_telegram_start_surfaces_ssl_failure_as_itself(caplog, monkeypatch):
    from app.models.settings import RunMethod
    from app.telegram import TelegramBotManager, session as session_module
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    def broken_ssl():
        raise ssl.SSLError("ssl context")

    monkeypatch.setattr(session_module, "create_outbound_ssl_context", broken_ssl)
    caplog.set_level(logging.DEBUG)
    manager = TelegramBotManager()
    settings = SimpleNamespace(
        method=RunMethod.WEBHOOK,
        proxy_url=f"socks5://{USER}:{SECRET}@{HOST}:1080",
        token="0:" + "t" * 8,
        webhook_url=f"https://{HOST}",
    )
    with pytest.raises(ssl.SSLError):
        await manager._start_locked(settings)
    assert not any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)


@pytest.mark.asyncio
async def test_r4_webhook_job_surfaces_ssl_failure_as_itself(caplog, monkeypatch):
    import app.jobs.send_notifications as job
    from app.utils import http_client
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    def broken_ssl():
        raise ssl.SSLError("ssl context")

    stored = SimpleNamespace(
        enable=True, proxy_url=f"socks5://{USER}:{SECRET}@{HOST}:1080", recurrent=3, timeout=1, webhooks=[]
    )
    monkeypatch.setattr(job, "webhook_settings", AsyncMock(return_value=stored))
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value=None)
    monkeypatch.setattr(job, "get_webhook_queue", lambda: queue)
    monkeypatch.setattr(http_client, "create_outbound_ssl_context", broken_ssl)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ssl.SSLError):
        await asyncio.wait_for(job.send_notifications(), timeout=10)
    assert not any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)


@pytest.mark.asyncio
async def test_r4_notification_client_surfaces_ssl_failure_as_itself(caplog, monkeypatch):
    import app.notification.client as client_module
    from app.utils import http_client
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    def broken_ssl():
        raise ssl.SSLError("ssl context")

    stored = SimpleNamespace(proxy_url=f"socks5://{USER}:{SECRET}@{HOST}:1080")
    monkeypatch.setattr(client_module, "notification_settings", AsyncMock(return_value=stored))
    monkeypatch.setattr(client_module, "client", None)
    monkeypatch.setattr(http_client, "create_outbound_ssl_context", broken_ssl)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(ssl.SSLError):
        await client_module.define_client()
    assert not any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", WIDE_URLS[:4])
async def test_r4_proxy_failures_still_reach_callers_as_invalid_proxy(url, caplog, monkeypatch):
    import app.notification.client as client_module
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    monkeypatch.setattr(client_module, "notification_settings", AsyncMock(return_value=SimpleNamespace(proxy_url=url)))
    monkeypatch.setattr(client_module, "client", None)
    caplog.set_level(logging.DEBUG)
    await client_module.define_client()
    assert client_module.client is None
    assert any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)
    assert all(SECRET not in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_r2_bulk_connect_isolates_a_node_with_bad_stored_proxy(monkeypatch):
    import app.operation.node as op_module
    from app.db.models import NodeStatus
    from app.operation.node import NodeOperation
    from app.utils.http_client import INVALID_PROXY_MESSAGE, InvalidProxyURL

    async def register(node):
        if node.id == 0:
            raise InvalidProxyURL(INVALID_PROXY_MESSAGE)
        return True

    async def connect(db_node, core, users, extra_cores=None, *, force_start=False):
        return {
            "node_id": db_node.id,
            "status": NodeStatus.connected,
            "message": "",
            "xray_version": "",
            "node_version": "",
            "old_status": NodeStatus.connecting,
        }

    status_update = AsyncMock()
    monkeypatch.setattr(NodeOperation, "_register_if_wanted", staticmethod(register))
    monkeypatch.setattr(NodeOperation, "_get_core_users_map", AsyncMock(return_value=({1: object()}, {1: []})))
    monkeypatch.setattr(NodeOperation, "connect_node", staticmethod(connect))
    monkeypatch.setattr(op_module, "bulk_update_node_status", status_update)
    monkeypatch.setattr(op_module.notification, "connect_node", AsyncMock())
    monkeypatch.setattr(op_module.notification, "error_node", AsyncMock())
    nodes = [
        SimpleNamespace(
            id=i, status=NodeStatus.connecting, core_config_id=1, additional_core_config_ids=None, name=f"n{i}"
        )
        for i in range(3)
    ]
    by_id = {node.id: node for node in nodes}
    monkeypatch.setattr(
        op_module, "get_node_by_id", AsyncMock(side_effect=lambda db, node_id, **kwargs: by_id.get(node_id))
    )
    await NodeOperation.__new__(NodeOperation)._connect_nodes_bulk_local(MagicMock(), nodes)
    results = {result["node_id"]: result for result in status_update.await_args.args[1]}
    assert results[0]["status"] == NodeStatus.error
    assert results[0]["message"] == INVALID_PROXY_MESSAGE
    assert results[1]["status"] == NodeStatus.connected
    assert results[2]["status"] == NodeStatus.connected
