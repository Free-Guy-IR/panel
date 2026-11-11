import asyncio
import contextlib
import logging

from aiohttp import web

from app.jobs import send_notifications as webhook_job
from app.models.settings import NotificationSettings, Webhook
from app.notification import client as notification_client, queue_manager
from app.notification.nats_queue import InMemoryNotificationQueue
from app.notification.queue_manager import enqueue_discord, enqueue_webhook

SECRET = "pw" + "Leak" + "Marker" + "9"


@contextlib.asynccontextmanager
async def _receiver():
    received: list[bytes] = []

    async def hook(request):
        received.append(await request.read())
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


async def test_a_destination_whose_request_cannot_be_built_does_not_stop_the_others(monkeypatch, caplog):
    queue = InMemoryNotificationQueue()
    monkeypatch.setattr(queue_manager, "webhook_queue_instance", queue)
    async with _receiver() as (url, received):
        stored = Webhook(
            enable=True,
            webhooks=[{"url": url, "secret": "healthy"}, {"url": f"{url}?broken", "secret": f"{SECRET}\nX"}],
            timeout=1,
            recurrent=1,
        )

        async def settings():
            return stored

        monkeypatch.setattr(webhook_job, "webhook_settings", settings)
        caplog.set_level(logging.DEBUG)
        await enqueue_webhook({"event": "probe"})
        await asyncio.wait_for(webhook_job.send_notifications(), timeout=15)

    assert len(received) == 1
    assert await queue.dequeue(timeout=0) is None
    assert any(record.getMessage() == "Webhook delivery failed: ValueError" for record in caplog.records)
    assert SECRET not in caplog.text


async def test_a_notification_whose_session_could_not_be_built_is_retried_then_dropped(monkeypatch, caplog):
    queue = InMemoryNotificationQueue()
    monkeypatch.setattr(queue_manager, "queue_instance", queue)
    stored = NotificationSettings.model_validate(
        {
            "notify_discord": True,
            "discord_webhook_url": "https://discord.com/api/webhooks/1/probe",
            "max_retries": 2,
            "proxy_url": f"http://user:{SECRET}@proxy.example:99999",
        }
    )

    async def settings():
        return stored

    monkeypatch.setattr(notification_client, "notification_settings", settings)
    monkeypatch.setattr(notification_client, "client", None)
    caplog.set_level(logging.DEBUG)
    await notification_client.define_client()
    assert notification_client.client is None
    await enqueue_discord({"content": "probe"}, "https://discord.com/api/webhooks/1/probe")

    first = await queue.dequeue(timeout=0)
    await asyncio.wait_for(notification_client.process_notification(first), timeout=5)
    second = await queue.dequeue(timeout=3)
    await asyncio.wait_for(notification_client.process_notification(second), timeout=5)

    assert await queue.dequeue(timeout=0) is None
    failures = [
        record for record in caplog.records if record.getMessage() == "Notification delivery failed: AttributeError"
    ]
    assert len(failures) == 2
    assert any(record.getMessage() == "Notification exhausted its delivery attempts" for record in caplog.records)
    assert SECRET not in caplog.text
