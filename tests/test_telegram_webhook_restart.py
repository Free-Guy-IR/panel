from types import SimpleNamespace

import pytest

from app import telegram as telegram_module
from app.models.settings import RunMethod
from app.nats.kv_cas import MemoryCasKv


class _TelegramServer:
    def __init__(self):
        self.webhook_url: str | None = None
        self.calls: list[str] = []


class _SharedKV(MemoryCasKv):
    async def put(self, key, value):
        self._rev += 1
        self._data[key] = (value, self._rev)
        return self._rev


class _Session:
    async def close(self):
        return None


class _Conn:
    async def close(self):
        return None


def _install(monkeypatch: pytest.MonkeyPatch, *, nats_enabled: bool) -> tuple[_TelegramServer, dict]:
    server = _TelegramServer()
    current = {"settings": None}
    kv = _SharedKV()

    class _Bot:
        def __init__(self, token, session, default):
            self.token = token
            self.session = session

        async def set_webhook(self, url, secret_token=None, allowed_updates=None, drop_pending_updates=None):
            server.calls.append(f"set:{self.token}")
            server.webhook_url = url

        async def delete_webhook(self, drop_pending_updates=None):
            server.calls.append(f"delete:{self.token}")
            server.webhook_url = None

        async def get_webhook_info(self):
            server.calls.append(f"info:{self.token}")
            return SimpleNamespace(url=server.webhook_url or "")

    async def telegram_settings():
        return current["settings"]

    async def setup_nats_kv(bucket):
        return _Conn(), None, kv

    monkeypatch.setattr(telegram_module, "Bot", _Bot)
    monkeypatch.setattr(telegram_module, "NativeTLSAiohttpSession", lambda proxy=None: _Session())
    monkeypatch.setattr(telegram_module, "include_routers", lambda dp: None)
    monkeypatch.setattr(telegram_module, "setup_middlewares", lambda dp: None)
    monkeypatch.setattr(telegram_module, "telegram_settings", telegram_settings)
    monkeypatch.setattr(telegram_module, "setup_nats_kv", setup_nats_kv)
    monkeypatch.setattr(telegram_module, "is_nats_enabled", lambda: nats_enabled)
    return server, current


def _settings(url: str = "https://panel.invalid", *, enable: bool = True, token: str = "123:abc"):
    return SimpleNamespace(
        enable=enable,
        token=token,
        proxy_url=None,
        method=RunMethod.WEBHOOK,
        webhook_url=url,
        webhook_secret="secret",
    )


async def _worker(force: bool = True) -> telegram_module.TelegramBotManager:
    manager = telegram_module.TelegramBotManager()
    await manager.sync_from_settings(force=force)
    return manager


@pytest.mark.asyncio
async def test_the_webhook_is_still_set_after_every_worker_restarts(monkeypatch: pytest.MonkeyPatch):
    server, current = _install(monkeypatch, nats_enabled=True)
    current["settings"] = _settings()

    workers = [await _worker(), await _worker()]
    assert server.webhook_url == "https://panel.invalid/api/tghook"
    for worker in workers:
        await worker.shutdown()

    restarted = [await _worker(), await _worker()]
    try:
        assert server.webhook_url == "https://panel.invalid/api/tghook", (
            f"after a restart of all workers Telegram has no webhook (calls: {server.calls}); every worker deleted "
            "it on shutdown and the persistent fingerprint told every restarted worker it was already set"
        )
    finally:
        for worker in restarted:
            await worker.shutdown()


@pytest.mark.asyncio
async def test_a_slower_worker_does_not_delete_the_webhook_another_worker_just_set(monkeypatch: pytest.MonkeyPatch):
    server, current = _install(monkeypatch, nats_enabled=True)
    current["settings"] = _settings("https://old.invalid")
    fast, slow = await _worker(), await _worker()

    current["settings"] = _settings("https://new.invalid")
    await fast.sync_from_settings()
    await slow.sync_from_settings()
    try:
        assert server.webhook_url == "https://new.invalid/api/tghook", (
            f"the slower worker deleted the webhook the first worker had just set (calls: {server.calls})"
        )
    finally:
        await fast.shutdown()
        await slow.shutdown()


@pytest.mark.asyncio
async def test_disabling_the_bot_removes_the_webhook_once_and_enabling_it_sets_it_again(
    monkeypatch: pytest.MonkeyPatch,
):
    server, current = _install(monkeypatch, nats_enabled=True)
    current["settings"] = _settings()
    workers = [await _worker(), await _worker()]

    current["settings"] = _settings(enable=False)
    for worker in workers:
        await worker.sync_from_settings()
    assert server.webhook_url is None
    assert server.calls.count("delete:123:abc") == 1

    current["settings"] = _settings()
    for worker in workers:
        await worker.sync_from_settings()
    try:
        assert server.webhook_url == "https://panel.invalid/api/tghook"
    finally:
        for worker in workers:
            await worker.shutdown()


@pytest.mark.asyncio
async def test_a_single_process_panel_still_sets_on_start_and_deletes_on_stop(monkeypatch: pytest.MonkeyPatch):
    server, current = _install(monkeypatch, nats_enabled=False)
    current["settings"] = _settings()

    worker = await _worker()
    assert server.webhook_url == "https://panel.invalid/api/tghook"
    await worker.shutdown()

    assert server.webhook_url is None
    assert server.calls == ["set:123:abc", "delete:123:abc"]
