import asyncio
import logging
import traceback
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.fork.operation.node_messages import scrub_credentials
from app.models.validators import ProxyValidator

SECRET = "pw" + "Leak" + "Marker" + str(7)
USER = "u" + "x"
HOST = "proxy.example"
BREAKING = ["[", "]", "\\", "/", "?", "#"]
CONFUSABLES = ["\uff03", "\uff0f", "\uff1f", "\uff20", "\uff1a", "\uff3b", "\uff3d", "\uff3c"]


def _bad_urls() -> list[str]:
    urls = []
    for char in BREAKING + CONFUSABLES:
        urls.append(f"http://{USER}:{SECRET}{char}@{HOST}:8080")
        urls.append(f"socks5://{USER}{char}:{SECRET}@{HOST}:1080")
    for port in ("0", "65536", "99999"):
        urls.append(f"http://{USER}:{SECRET}@{HOST}:{port}")
    urls.append(f"http://{USER}:{SECRET}@{HOST}:8080\n")
    return urls


BAD_URLS = _bad_urls()


def _chain_text(error: BaseException | None) -> str:
    if error is None:
        return ""
    parts = []
    seen = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        parts.append(str(current))
        parts.append(repr(current.args))
        current = current.__cause__ or current.__context__
    parts.append("".join(traceback.format_exception(error)))
    return "\n".join(parts)


def _record_text(record: logging.LogRecord) -> str:
    parts = [record.getMessage(), repr(record.args), logging.Formatter().format(record)]
    if record.exc_info and record.exc_info[1] is not None:
        parts.append(_chain_text(record.exc_info[1]))
    if record.exc_text:
        parts.append(record.exc_text)
    if record.stack_info:
        parts.append(record.stack_info)
    return "\n".join(parts)


def _assert_nothing_leaked(caplog, url: str, error: BaseException | None) -> None:
    for record in caplog.records:
        text = _record_text(record)
        assert SECRET not in text, record.name
        assert url not in text, record.name
    text = _chain_text(error)
    assert SECRET not in text
    assert url not in text


@pytest.mark.parametrize("url", BAD_URLS)
def test_validator_rejects_breaking_userinfo_without_echoing_value(url):
    with pytest.raises(ValueError) as caught:
        ProxyValidator.validate_proxy_url(url)
    assert SECRET not in _chain_text(caught.value)
    assert url not in _chain_text(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        f"http://{USER}:{SECRET}@{HOST}:8080",
        f"socks5://{USER}:{SECRET}%5B@{HOST}:1080",
        f"socks5://{USER}:{SECRET}é@{HOST}:1080",
        f"https://{HOST}:443",
        "socks4://127.0.0.1:1080",
        f"socks5://{USER}:{SECRET}@{HOST}:65535",
        f"http://{HOST}:1",
    ],
)
def test_validator_keeps_existing_valid_forms(url):
    assert ProxyValidator.validate_proxy_url(url) == url


@pytest.mark.parametrize("char", ["[", "]", "\\"])
def test_log_scrub_masks_breaking_userinfo(char):
    url = f"http://{USER}:{SECRET}{char}@{HOST}:8080"
    scrubbed = scrub_credentials(f"proxy failed {url} now")
    assert SECRET not in scrubbed


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_URLS)
async def test_outbound_session_never_leaks_stored_bad_proxy(url, caplog):
    from app.utils.http_client import create_outbound_http_session

    caplog.set_level(logging.DEBUG)
    error = None
    try:
        session = create_outbound_http_session(proxy=url, timeout_seconds=1)
        try:
            await session.get("http://127.0.0.1:9/")
        finally:
            await session.close()
    except Exception as exc:
        error = exc
    _assert_nothing_leaked(caplog, url, error)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_URLS)
async def test_telegram_session_never_leaks_stored_bad_proxy(url, caplog):
    from app.telegram.session import NativeTLSAiohttpSession

    caplog.set_level(logging.DEBUG)
    error = None
    try:
        session = NativeTLSAiohttpSession(proxy=url)
        try:
            await session.create_session()
        finally:
            await session.close()
    except Exception as exc:
        error = exc
    _assert_nothing_leaked(caplog, url, error)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_URLS)
async def test_notification_client_never_leaks_stored_bad_proxy(url, caplog, monkeypatch):
    import app.notification.client as notification_client

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        notification_client, "notification_settings", AsyncMock(return_value=SimpleNamespace(proxy_url=url))
    )
    monkeypatch.setattr(notification_client, "client", None)
    error = None
    try:
        await notification_client.define_client()
        created = notification_client.client
        if created is not None:
            try:
                await created.get("http://127.0.0.1:9/")
            finally:
                await created.close()
    except Exception as exc:
        error = exc
    finally:
        monkeypatch.setattr(notification_client, "client", None)
    _assert_nothing_leaked(caplog, url, error)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_URLS)
async def test_webhook_job_never_leaks_stored_bad_proxy(url, caplog, monkeypatch):
    import app.jobs.send_notifications as job

    caplog.set_level(logging.DEBUG)
    stored = SimpleNamespace(enable=True, proxy_url=url, recurrent=3, timeout=1, webhooks=[])
    monkeypatch.setattr(job, "webhook_settings", AsyncMock(return_value=stored))
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value=None)
    queue.ack = AsyncMock()
    monkeypatch.setattr(job, "get_webhook_queue", lambda: queue)
    error = None
    try:
        await asyncio.wait_for(job.send_notifications(), timeout=10)
    except Exception as exc:
        error = exc
    _assert_nothing_leaked(caplog, url, error)


@pytest.mark.asyncio
async def test_notification_client_reports_fixed_message_for_bad_proxy(caplog, monkeypatch):
    import app.notification.client as notification_client
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    url = BAD_URLS[0]
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(
        notification_client, "notification_settings", AsyncMock(return_value=SimpleNamespace(proxy_url=url))
    )
    monkeypatch.setattr(notification_client, "client", None)
    await notification_client.define_client()
    assert notification_client.client is None
    assert any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)
    _assert_nothing_leaked(caplog, url, None)


@pytest.mark.parametrize("url", BAD_URLS)
def test_checked_proxy_url_rejects_with_fixed_message_only(url):
    from app.utils.http_client import INVALID_PROXY_MESSAGE, InvalidProxyURL, checked_proxy_url

    with pytest.raises(InvalidProxyURL) as caught:
        checked_proxy_url(url)
    assert str(caught.value) == INVALID_PROXY_MESSAGE
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert SECRET not in _chain_text(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        f"http://{USER}:{SECRET}\uff03@{HOST}:8080",
        f"socks5://{USER}:{SECRET}\uff20@{HOST}:1080",
        f"http://{USER}:{SECRET}@{HOST}:99999",
        f"http://{USER}:{SECRET}@{HOST}:0",
        f"http://{USER}:{SECRET}]@{HOST}:8080",
    ],
)
def test_checked_proxy_url_parses_even_when_the_validator_lets_a_value_through(url, monkeypatch):
    from app.utils import http_client

    monkeypatch.setattr(http_client.ProxyValidator, "validate_proxy_url", staticmethod(lambda value: value))
    with pytest.raises(http_client.InvalidProxyURL) as caught:
        http_client.checked_proxy_url(url)
    assert str(caught.value) == http_client.INVALID_PROXY_MESSAGE
    assert caught.value.__context__ is None
    assert SECRET not in _chain_text(caught.value)


def test_checked_proxy_url_keeps_valid_and_empty_values():
    from app.utils.http_client import checked_proxy_url

    url = f"socks5://{USER}:{SECRET}@{HOST}:1080"
    assert checked_proxy_url(url) == url
    assert checked_proxy_url(None) is None
    assert checked_proxy_url("") is None


def _telegram_settings(url):
    from app.models.settings import RunMethod

    return SimpleNamespace(method=RunMethod.WEBHOOK, proxy_url=url, token="0:" + "t" * 8, webhook_url=f"https://{HOST}")


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_URLS)
async def test_telegram_start_logs_fixed_message_for_bad_stored_proxy(url, caplog):
    from app.telegram import TelegramBotManager
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    caplog.set_level(logging.DEBUG)
    manager = TelegramBotManager()
    error = None
    try:
        await manager._start_locked(_telegram_settings(url))
    except Exception as exc:
        error = exc
    assert error is None
    assert manager._bot is None
    assert any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)
    _assert_nothing_leaked(caplog, url, error)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError, RuntimeError, TypeError])
async def test_telegram_start_surfaces_non_proxy_session_error_as_itself(failure, caplog, monkeypatch):
    from app import telegram
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    url = f"socks5://{USER}:{SECRET}@{HOST}:1080"

    def broken_session(*args, **kwargs):
        raise failure("session construction failed")

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(telegram, "NativeTLSAiohttpSession", broken_session)
    manager = telegram.TelegramBotManager()
    with pytest.raises(failure) as caught:
        await manager._start_locked(_telegram_settings(url))
    assert manager._bot is None
    assert not any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)
    _assert_nothing_leaked(caplog, url, caught.value)


def _webhook_job(monkeypatch, url):
    import app.jobs.send_notifications as job

    stored = SimpleNamespace(enable=True, proxy_url=url, recurrent=3, timeout=1, webhooks=[])
    monkeypatch.setattr(job, "webhook_settings", AsyncMock(return_value=stored))
    queue = MagicMock()
    queue.dequeue = AsyncMock(return_value=None)
    queue.ack = AsyncMock()
    monkeypatch.setattr(job, "get_webhook_queue", lambda: queue)
    return job, queue


@pytest.mark.asyncio
@pytest.mark.parametrize("url", BAD_URLS)
async def test_webhook_job_logs_fixed_message_and_skips_run_for_bad_proxy(url, caplog, monkeypatch):
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    caplog.set_level(logging.DEBUG)
    job, queue = _webhook_job(monkeypatch, url)
    error = None
    try:
        await asyncio.wait_for(job.send_notifications(), timeout=10)
    except Exception as exc:
        error = exc
    assert error is None
    queue.dequeue.assert_not_awaited()
    assert any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)
    _assert_nothing_leaked(caplog, url, error)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError, RuntimeError, TypeError])
async def test_webhook_job_surfaces_non_proxy_session_error_as_itself(failure, caplog, monkeypatch):
    from app.utils.http_client import INVALID_PROXY_MESSAGE

    url = f"socks5://{USER}:{SECRET}@{HOST}:1080"

    def broken_session(*args, **kwargs):
        raise failure("session construction failed")

    caplog.set_level(logging.DEBUG)
    job, queue = _webhook_job(monkeypatch, url)
    monkeypatch.setattr(job, "create_outbound_http_session", broken_session)
    with pytest.raises(failure) as caught:
        await asyncio.wait_for(job.send_notifications(), timeout=10)
    queue.dequeue.assert_not_awaited()
    assert not any(record.getMessage() == INVALID_PROXY_MESSAGE for record in caplog.records)
    _assert_nothing_leaked(caplog, url, caught.value)


def _node_row(proxy_url):
    from app.db.models import Node

    node = Node(
        name="n1", address="10.0.0.1", port=1000, api_port=1001, server_ca="ca", api_key="key", core_config_id=None
    )
    node.id = 7
    node.proxy_url = proxy_url
    return node


@pytest.mark.parametrize("url", BAD_URLS)
def test_node_kwargs_reject_bad_stored_proxy_with_fixed_message(url):
    from app.node import NodeManager
    from app.utils.http_client import INVALID_PROXY_MESSAGE, InvalidProxyURL

    with pytest.raises(InvalidProxyURL) as caught:
        NodeManager()._create_node_kwargs(_node_row(url))
    assert str(caught.value) == INVALID_PROXY_MESSAGE
    assert SECRET not in _chain_text(caught.value)


@pytest.mark.parametrize("url", [None, f"socks5://{USER}:{SECRET}@{HOST}:1080"])
def test_node_kwargs_keep_valid_and_empty_proxy(url):
    from app.node import NodeManager

    assert NodeManager()._create_node_kwargs(_node_row(url))["proxy"] == url


@pytest.mark.asyncio
async def test_node_update_with_bad_stored_proxy_keeps_the_live_node(monkeypatch):
    import app.node as node_module
    from app.utils.http_client import InvalidProxyURL

    monkeypatch.setattr(node_module, "ensure_bridge_memory", AsyncMock())
    created = MagicMock()
    monkeypatch.setattr(node_module, "create_node", MagicMock(return_value=created))
    manager = node_module.NodeManager()
    manager._shutdown_node = AsyncMock()
    good = _node_row(f"socks5://{USER}:{SECRET}@{HOST}:1080")
    assert await manager.update_node(good) is created
    manager._shutdown_node.reset_mock()
    bad = _node_row(f"socks5://{USER}:{SECRET}\uff03@{HOST}:1080")
    with pytest.raises(InvalidProxyURL):
        await manager.update_node(bad)
    assert manager._nodes[7] is created
    manager._shutdown_node.assert_not_awaited()
