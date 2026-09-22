import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from app.fork.subscription.helpers import UnissuedSecretError
from app.fork.subscription.l2tp import L2TPConfiguration
from app.fork.subscription.openvpn import OpenVPNConfiguration
from app.models.proxy import ProxyTable
from app.models.settings import ConfigFormat
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.models.user import UsersResponseWithInbounds
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.subscription.config_cache import clear_sub_config_cache

OVPN = "ovpn-udp"
L2TP = "l2tp-de"
MTP = "mtp-443"
VLESS = "vless-in"
VLESS_ID = "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----"
TLS_CRYPT = "-----BEGIN OpenVPN Static key V1-----\nabcd\n-----END OpenVPN Static key V1-----"


def _host(tag, protocol, port, finalmask=None, network="udp"):
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
    1: _host(OVPN, "openvpn", 1194, {"openvpn": {"ca_cert": CA, "tls_crypt_key": TLS_CRYPT}}),
    2: _host(L2TP, "l2tp", 1701, {"l2tp": {"server_addr": "vpn.example.com", "psk": "correct-horse"}}),
    3: _host(MTP, "mtproto", 443, {"mtproto": {"mode": "plain"}}),
    4: _host(VLESS, "vless", 443, network="tcp"),
}


def _user(openvpn=None, l2tp=None, mtproto=None, inbounds=(OVPN, L2TP, MTP, VLESS)):
    settings = ProxyTable().dict()
    settings["vless"] = {"id": VLESS_ID, "flow": ""}
    settings["openvpn"] = {"password": openvpn}
    settings["l2tp"] = {"password": l2tp}
    settings["mtproto"] = {"secret": mtproto}
    return UsersResponseWithInbounds.model_validate(
        {
            "id": 41,
            "username": "subscriber",
            "status": "active",
            "used_traffic": 0,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            "proxy_settings": settings,
            "group_ids": [],
            "inbounds": list(inbounds),
        }
    )


def _issued():
    return _user(openvpn="ovpn-pass-123", l2tp="L2tpPass12345678", mtproto="0123456789abcdef0123456789abcdef")


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
def fresh_warning_memory(monkeypatch):
    from app.fork.subscription import helpers

    monkeypatch.setattr(helpers, "_unissued_warned_at", {})


def _capture_subscription_warnings():
    records = []
    handler = logging.Handler(level=logging.WARNING)
    handler.emit = records.append
    logging.getLogger("subscription").addHandler(handler)
    return records, handler


@pytest.fixture(autouse=True)
def served(monkeypatch):
    from app.operation import subscription as operation_module
    from app.subscription import share

    async def get_hosts():
        return dict(HOSTS)

    async def settings():
        return _settings()

    async def templates():
        return {"USER_AGENT_TEMPLATE": "", "GRPC_USER_AGENT_TEMPLATE": ""}

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
async def test_the_links_format_is_untouched_by_a_missing_openvpn_or_l2tp_secret():
    operation = _operation()

    missing, missing_type = await operation.fetch_config(
        _user(mtproto="0123456789abcdef0123456789abcdef"), ConfigFormat.links
    )
    clear_sub_config_cache()
    issued, issued_type = await operation.fetch_config(_issued(), ConfigFormat.links)

    assert missing_type == issued_type == "text/plain"
    assert missing == issued
    assert VLESS_ID in missing
    assert "tg://proxy" in missing


@pytest.mark.asyncio
async def test_a_missing_mtproto_secret_drops_only_that_link_and_is_logged():
    records, handler = _capture_subscription_warnings()
    try:
        body, _ = await _operation().fetch_config(_user(), ConfigFormat.links)
    finally:
        logging.getLogger("subscription").removeHandler(handler)

    assert VLESS_ID in body
    assert "tg://proxy" not in body
    messages = [record.getMessage() for record in records]
    assert any("user 41 can reach mtproto inbound 'mtp-443'" in message for message in messages)


@pytest.mark.asyncio
async def test_repeated_refreshes_warn_once_per_user_and_protocol_but_refuse_every_time():
    records, handler = _capture_subscription_warnings()
    try:
        for _ in range(3):
            clear_sub_config_cache()
            with pytest.raises(UnissuedSecretError):
                await _operation().fetch_config(_user(), ConfigFormat.openvpn)
            clear_sub_config_cache()
            with pytest.raises(UnissuedSecretError):
                await _operation().fetch_config(_user(), ConfigFormat.l2tp)
    finally:
        logging.getLogger("subscription").removeHandler(handler)

    messages = [record.getMessage() for record in records]
    assert len([m for m in messages if "can reach openvpn inbound" in m]) == 1
    assert len([m for m in messages if "can reach l2tp inbound" in m]) == 1


def test_the_warning_comes_back_after_the_interval(monkeypatch):
    from app.fork.subscription import helpers

    clock = iter([1000.0, 1001.0, 1000.0 + helpers.UNISSUED_WARNING_INTERVAL_SECONDS + 1])
    monkeypatch.setattr(helpers.time, "monotonic", lambda: next(clock))
    records, handler = _capture_subscription_warnings()
    try:
        for _ in range(3):
            helpers._warn_unissued_secret("openvpn", 41, OVPN)
    finally:
        logging.getLogger("subscription").removeHandler(handler)

    assert len(records) == 2


@pytest.mark.asyncio
async def test_an_entitled_user_with_a_password_is_served_a_real_openvpn_profile():
    body, media_type = await _operation().fetch_config(_issued(), ConfigFormat.openvpn)

    assert media_type == "application/zip"
    assert body


@pytest.mark.asyncio
async def test_the_served_openvpn_format_refuses_instead_of_answering_an_empty_profile():
    with pytest.raises(HTTPException) as raised:
        await _operation().fetch_config(_user(), ConfigFormat.openvpn)

    assert isinstance(raised.value, UnissuedSecretError)
    assert raised.value.status_code == 409
    assert raised.value.detail.startswith("OpenVPN has not been activated for this user yet")


@pytest.mark.asyncio
async def test_the_served_l2tp_format_refuses_instead_of_answering_an_empty_list():
    with pytest.raises(HTTPException) as raised:
        await _operation().fetch_config(_user(), ConfigFormat.l2tp)

    assert isinstance(raised.value, UnissuedSecretError)
    assert raised.value.status_code == 409
    assert raised.value.detail.startswith("L2TP has not been activated for this user yet")


@pytest.mark.asyncio
async def test_a_user_who_cannot_reach_l2tp_keeps_the_old_empty_list():
    body, media_type = await _operation().fetch_config(_user(inbounds=(VLESS,)), ConfigFormat.l2tp)

    assert media_type == "application/json"
    assert body == "[]"


def test_every_registered_l2tp_and_openvpn_factory_refuses():
    from app.fork.registry import get_subscription_format

    assert get_subscription_format("l2tp")().refuse_unissued_secret is True
    assert get_subscription_format("openvpn")().refuse_unissued_secret is True


@pytest.mark.asyncio
async def test_a_user_who_cannot_reach_openvpn_keeps_the_old_empty_answer():
    body, media_type = await _operation().fetch_config(_user(inbounds=(VLESS,)), ConfigFormat.openvpn)

    assert media_type == "application/zip"
    assert body == b""


def test_an_openvpn_builder_with_nothing_noted_still_renders_empty():
    assert OpenVPNConfiguration().render() == b""


@pytest.mark.asyncio
async def test_the_page_builders_never_raise_for_a_missing_secret():
    operation = _operation()

    assert await operation.build_openvpn_files(_user()) == []
    assert await operation.build_l2tp_details(_user()) == []


@pytest.mark.asyncio
async def test_the_page_builders_return_the_real_entries_when_issued():
    operation = _operation()

    files = await operation.build_openvpn_files(_issued())
    details = await operation.build_l2tp_details(_issued())

    assert [entry["filename"] for entry in files] == ["openvpn-udp.ovpn"]
    assert details == [
        {
            "remark": "l2tp host",
            "server": "203.0.113.10",
            "username": "41",
            "password": "L2tpPass12345678",
            "secret": "correct-horse",
            "dns": [],
        }
    ]


@pytest.mark.asyncio
async def test_the_page_l2tp_details_match_what_the_served_l2tp_format_renders():
    import json

    served, _ = await _operation().fetch_config(_issued(), ConfigFormat.l2tp)

    assert await _operation().build_l2tp_details(_issued()) == json.loads(served)


@pytest.mark.asyncio
async def test_the_html_page_still_renders_when_openvpn_and_l2tp_are_not_activated(monkeypatch):
    from app.operation import subscription as operation_module

    operation = _operation()
    captured = {}
    db_user = SimpleNamespace(id=41, admin=SimpleNamespace(sub_template=None, role=None))

    async def get_validated_sub(db, token, **kwargs):
        return db_user

    async def validated_user(user):
        return _user(mtproto="0123456789abcdef0123456789abcdef")

    async def hwid_enabled(user, db=None):
        return False

    async def format_variables(user):
        return {}

    async def hwid_settings():
        return SimpleNamespace(require_hwid_for_manual_sub=False)

    async def queue_access(*args, **kwargs):
        return None

    def build_payload(user, links, announce, sub_settings, variables, is_hwid_enabled, **kwargs):
        captured.update(kwargs, links=links)
        return {}

    operation.get_validated_sub = get_validated_sub
    operation.validated_user = validated_user
    operation.is_user_hwid_enabled = hwid_enabled
    operation.get_format_variables = format_variables
    operation._build_subscription_body_payload = build_payload
    monkeypatch.setattr(operation_module, "hwid_settings", hwid_settings)
    monkeypatch.setattr(operation_module, "queue_subscription_access", queue_access)
    monkeypatch.setattr(operation_module, "render_template", lambda template, payload: "<html>page</html>")

    response = await operation.user_subscription(db=None, token="token", accept_header="text/html")

    assert isinstance(response, HTMLResponse)
    assert response.status_code == 200
    assert captured["openvpn_configs"] == []
    assert captured["has_openvpn"] is False
    assert captured["l2tp_details"] == []
    assert any(VLESS_ID in link for link in captured["links"])


def test_the_l2tp_builder_keeps_its_empty_list_for_a_missing_password():
    conf = L2TPConfiguration()
    conf.add("x", "203.0.113.10", HOSTS[2], {"_user_id": 41, "password": None})

    assert conf.render() == "[]"
    assert conf.unissued_secret_protocols == {"l2tp"}
