import asyncio
from types import SimpleNamespace

import pytest

from app.models.proxy import ProxyTable
from app.subscription import config_cache, share


def _user(**overrides):
    fields = {
        "id": 7,
        "username": "cache_user",
        "created_at": None,
        "proxy_settings": ProxyTable(),
        "status": None,
        "data_limit": None,
        "expire": None,
        "inbounds": [],
        "admin": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def scripted_render(monkeypatch):
    async def no_templates():
        return {}

    async def no_settings():
        return SimpleNamespace(custom_variables=[])

    monkeypatch.setattr(share, "subscription_client_templates", no_templates)
    monkeypatch.setattr(share, "subscription_settings", no_settings)
    monkeypatch.setattr(share, "_build_subscription_config", lambda *_: object())
    monkeypatch.setattr(share, "setup_format_variables", lambda *_: {})
    config_cache.clear_sub_config_cache()
    yield monkeypatch
    config_cache.clear_sub_config_cache()


@pytest.mark.asyncio
async def test_a_render_in_flight_across_a_clear_is_not_served_afterwards(scripted_render):
    started = asyncio.Event()
    released = asyncio.Event()
    outputs = iter(["before-clear", "after-clear"])

    async def render(*_args, **_kwargs):
        value = next(outputs)
        if value == "before-clear":
            started.set()
            await released.wait()
        return value

    scripted_render.setattr(share, "process_inbounds_and_tags", render)
    user = _user()

    in_flight = asyncio.create_task(share.generate_subscription(user, "links", False))
    await started.wait()
    config_cache.clear_sub_config_cache()
    released.set()
    assert await in_flight == "before-clear"

    assert await share.generate_subscription(user, "links", False) == "after-clear"


@pytest.mark.asyncio
async def test_a_render_of_pre_rotation_secrets_is_never_served_for_the_rotated_user(scripted_render):
    started = asyncio.Event()
    released = asyncio.Event()

    async def render(user, *_args, **_kwargs):
        if user.proxy_settings.vless.id == original.proxy_settings.vless.id:
            started.set()
            await released.wait()
        return str(user.proxy_settings.vless.id)

    scripted_render.setattr(share, "process_inbounds_and_tags", render)
    original = _user()
    rotated = _user(proxy_settings=ProxyTable())

    in_flight = asyncio.create_task(share.generate_subscription(original, "links", False))
    await started.wait()
    served_after_rotation = await share.generate_subscription(rotated, "links", False)
    released.set()
    await in_flight

    assert served_after_rotation == str(rotated.proxy_settings.vless.id)
    assert await share.generate_subscription(rotated, "links", False) == str(rotated.proxy_settings.vless.id)


def _key(user) -> tuple:
    return config_cache.make_sub_config_key(user, "links", False, False)


def test_the_key_separates_users_that_share_an_id():
    deleted = _user(username="deleted_user")
    assert _key(deleted) != _key(_user(username="new_user", proxy_settings=deleted.proxy_settings))
    assert _key(deleted) != _key(_user(username="deleted_user"))
    assert _key(deleted) == _key(_user(username="deleted_user", proxy_settings=deleted.proxy_settings))
