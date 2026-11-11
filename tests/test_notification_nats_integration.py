import asyncio
import contextlib
import logging
import os
import shutil
import socket
import subprocess

import nats
import pytest
from aiohttp import web
from nats.js.api import DiscardPolicy, RetentionPolicy, StreamConfig

from app.models.settings import NotificationSettings
from app.notification import client as notification_client, queue_manager
from app.notification.nats_queue import NatsNotificationQueue

STREAM_LIMIT = 200
SHARE = 20
SHARE_DROPPED = (
    "Notification dropped: Discord webhook 111 already holds {} rate-limited notifications, its share of the queue"
)


@pytest.fixture
async def nats_url(tmp_path):
    binary = os.environ.get("NATS_SERVER_BINARY") or shutil.which("nats-server")
    if not binary:
        pytest.skip("Set NATS_SERVER_BINARY to run the notification JetStream tests")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = await asyncio.to_thread(
        subprocess.Popen,
        [binary, "-js", "-a", "127.0.0.1", "-p", str(port), "-sd", str(tmp_path / "nats")],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(10):
            while True:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError("Test NATS server exited during startup")
                    await asyncio.sleep(0.02)
        yield f"nats://127.0.0.1:{port}"
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait, timeout=10)


@contextlib.asynccontextmanager
async def _receivers(monkeypatch):
    seen = {"111": [], "333": []}

    async def hook(request):
        webhook = request.match_info["webhook"]
        seen[webhook].append((await request.json())["content"])
        if webhook == "111":
            return web.json_response({"retry_after": 3600}, status=429)
        return web.Response(status=204)

    application = web.Application()
    application.router.add_post("/api/webhooks/{webhook}/{token}", hook)
    runner = web.AppRunner(application, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    stored = NotificationSettings.model_validate(
        {"notify_discord": True, "discord_webhook_url": "https://discord.com/api/webhooks/1/probe", "max_retries": 3}
    )

    async def settings():
        return stored

    monkeypatch.setattr(notification_client, "notification_settings", settings)
    monkeypatch.setattr(notification_client, "client", None)
    await notification_client.define_client()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/api/webhooks"
    try:
        yield (lambda webhook: f"{base}/{webhook}/token"), seen
    finally:
        await notification_client.client.close()
        await runner.cleanup()


@contextlib.asynccontextmanager
async def _dispatcher(url, monkeypatch):
    from app.fork import notification_rate_limit

    monkeypatch.setattr(notification_client, "_rate_limits", {})
    notification_rate_limit._held.clear()
    nc = await nats.connect(url)
    queue = NatsNotificationQueue()
    queue._nc = nc
    queue._js = nc.jetstream()
    try:
        await queue._js.stream_info(queue.STREAM_NAME)
    except nats.js.errors.NotFoundError:
        await queue._js.add_stream(
            StreamConfig(
                name=queue.STREAM_NAME,
                subjects=[queue.SUBJECT],
                retention=RetentionPolicy.WORK_QUEUE,
                discard=DiscardPolicy.NEW,
                max_msgs=STREAM_LIMIT,
            )
        )
    await queue._initialize_stream(create_consumer=True)
    monkeypatch.setattr(queue_manager, "queue_instance", queue)
    task = asyncio.create_task(notification_client.run_notification_dispatcher())
    try:
        yield queue
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await nc.close()


async def _stored(queue) -> int:
    return (await queue._js.stream_info(queue.STREAM_NAME)).state.messages


async def _held(queue) -> int:
    return (await queue._js.stream_info(queue.RETRY_STREAM)).state.messages


def _share_drops(caplog) -> int:
    return sum(1 for record in caplog.records if record.getMessage().startswith("Notification dropped:"))


async def _settle(queue, caplog, held_before: int, drops_before: int, sent: int) -> None:
    async with asyncio.timeout(20):
        while (await _held(queue) - held_before) + (_share_drops(caplog) - drops_before) < sent:
            await asyncio.sleep(0.05)


async def test_a_dispatcher_restart_keeps_a_rate_limited_destination_at_its_share(nats_url, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    stored_after_each_start = []
    async with _receivers(monkeypatch) as (url, _):
        for start in range(3):
            async with _dispatcher(nats_url, monkeypatch) as queue:
                held, drops = await _held(queue), _share_drops(caplog)
                for number in range(25):
                    await queue_manager.enqueue_discord({"content": f"start {start} message {number}"}, url("111"))
                await _settle(queue, caplog, held, drops, 25)
                stored_after_each_start.append(await _stored(queue))

    assert stored_after_each_start == [SHARE, SHARE, SHARE]
    assert _share_drops(caplog) == 5 + 25 + 25
