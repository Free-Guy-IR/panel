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


def _bad_urls() -> list[str]:
    urls = []
    for char in BREAKING:
        urls.append(f"http://{USER}:{SECRET}{char}@{HOST}:8080")
        urls.append(f"socks5://{USER}{char}:{SECRET}@{HOST}:1080")
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
