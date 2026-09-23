from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models.proxy import ProxyTable
from app.models.settings import ConfigFormat
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.models.user import UsersResponseWithInbounds
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.subscription.config_cache import clear_sub_config_cache

MTP = "mtp-443"
VLESS = "vless-in"
VLESS_ID = "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"
SECRET = "0123456789abcdef0123456789abcdef"


def _host(tag, protocol, port, finalmask=None, network="tcp"):
    return SubscriptionInboundData(
        remark=f"{protocol} host",
        inbound_tag=tag,
        protocol=protocol,
        address=["203.0.113.10"],
        port=[port],
        network=network,
        tls_config=TLSConfig(),
        transport_config=TCPTransportConfig(path="", host=[]),
        finalmask=finalmask,
    )


HOSTS = {
    1: _host(MTP, "mtproto", 443, {"mtproto": {"mode": "plain"}}),
    2: _host(VLESS, "vless", 443),
}


def _user(inbounds, mtproto=None):
    settings = ProxyTable().dict()
    settings["vless"] = {"id": VLESS_ID, "flow": ""}
    settings["mtproto"] = {"secret": mtproto}
    return UsersResponseWithInbounds.model_validate(
        {
            "id": 57,
            "username": "telegram-only",
            "status": "active",
            "used_traffic": 0,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "proxy_settings": settings,
            "group_ids": [],
            "inbounds": list(inbounds),
        }
    )


def _settings():
    return SimpleNamespace(
        randomize_order=False,
        custom_variables=[],
        disable_sub_template=False,
        allow_browser_config=True,
        announce="",
        announce_url="",
        support_url="",
        profile_title="",
        update_interval=12,
        response_headers={},
        rules=[],
    )


@pytest.fixture(autouse=True)
def served(monkeypatch):
    from app.fork.subscription import helpers
    from app.operation import subscription as operation_module
    from app.subscription import share

    async def get_hosts():
        return dict(HOSTS)

    async def settings():
        return _settings()

    async def templates():
        return {"USER_AGENT_TEMPLATE": "", "GRPC_USER_AGENT_TEMPLATE": ""}

    monkeypatch.setattr(helpers, "_unissued_warned_at", {})
    monkeypatch.setattr(share, "host_manager", SimpleNamespace(get_hosts=get_hosts))
    monkeypatch.setattr(share, "subscription_settings", settings)
    monkeypatch.setattr(share, "subscription_client_templates", templates)
    monkeypatch.setattr(operation_module, "subscription_settings", settings)
    clear_sub_config_cache()
    yield
    clear_sub_config_cache()


def _operation():
    return SubscriptionOperation(operator_type=OperatorType.API)


@pytest.mark.asyncio
async def test_an_mtproto_only_user_without_a_secret_gets_409_on_links():
    with pytest.raises(HTTPException) as raised:
        await _operation().fetch_config(_user([MTP]), ConfigFormat.links)

    assert raised.value.status_code == 409
    assert raised.value.detail == (
        "MTProto has not been activated for this user yet; ask the administrator to activate it"
    )


@pytest.mark.asyncio
async def test_a_user_with_other_links_keeps_them_when_the_mtproto_secret_is_missing():
    body, media_type = await _operation().fetch_config(_user([MTP, VLESS]), ConfigFormat.links)

    assert media_type == "text/plain"
    assert VLESS_ID in body
    assert "tg://proxy" not in body


@pytest.mark.asyncio
async def test_a_user_with_a_secret_gets_the_mtproto_link():
    body, _ = await _operation().fetch_config(_user([MTP], mtproto=SECRET), ConfigFormat.links)

    assert "tg://proxy" in body
    assert SECRET in body
