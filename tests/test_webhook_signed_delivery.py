import asyncio
import contextlib
import hashlib
import hmac
import json

import pytest
from aiohttp import web

from app.jobs import send_notifications as webhook_job
from app.models.settings import Webhook
from app.notification import queue_manager
from app.notification.nats_queue import InMemoryNotificationQueue

SECRET = "receiver-" + "secret"


@contextlib.asynccontextmanager
async def _receiver():
    received: list[tuple[dict[str, str], bytes]] = []

    async def hook(request):
        received.append(({key.lower(): value for key, value in request.headers.items()}, await request.read()))
        return web.Response(status=204)

    application = web.Application()
    application.router.add_post("/hook", hook)
    runner = web.AppRunner(application)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/hook", received
    finally:
        await runner.cleanup()


async def _deliver(monkeypatch, url: str, secret: str, payloads: list[dict]) -> None:
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", InMemoryNotificationQueue())
    stored = Webhook(enable=True, webhooks=[{"url": url, "secret": secret}], timeout=1, recurrent=1)

    async def settings():
        return stored

    monkeypatch.setattr(webhook_job, "webhook_settings", settings)
    for payload in payloads:
        await queue_manager.enqueue_webhook(payload)
    await asyncio.wait_for(webhook_job.send_notifications(), timeout=15)


async def test_a_webhook_batch_is_signed_over_the_exact_body_the_receiver_gets(monkeypatch):
    payloads = [{"event": "user_created", "username": "alice"}, {"event": "user_deleted", "username": "bob"}]
    async with _receiver() as (url, received):
        await _deliver(monkeypatch, url, SECRET, payloads)

    assert len(received) == 1
    headers, body = received[0]
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert headers.get("x-webhook-signature") == f"sha256={expected}"
    assert headers.get("content-type") == "application/json"
    assert json.loads(body) == payloads


async def test_a_webhook_without_a_secret_carries_no_signature(monkeypatch):
    async with _receiver() as (url, received):
        await _deliver(monkeypatch, url, "", [{"event": "user_created"}])

    assert len(received) == 1
    headers, _ = received[0]
    assert "x-webhook-signature" not in headers
    assert "x-webhook-secret" not in headers


@pytest.mark.parametrize("secret", ["receiver-a", "receiver-b"])
async def test_each_destination_is_signed_with_its_own_secret(monkeypatch, secret):
    async with _receiver() as (url, received):
        await _deliver(monkeypatch, url, secret, [{"event": "user_created"}])

    headers, body = received[0]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert headers.get("x-webhook-signature") == f"sha256={expected}"
