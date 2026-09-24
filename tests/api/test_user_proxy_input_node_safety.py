from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import User, UserStatus
from app.models.proxy import ProxyTable
from app.node import sync as node_sync
from app.node.user import _serialize_user_for_node, serialize_users_for_node
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_core, create_group, delete_core, delete_group, unique_name

XRAY_SERVER_ACCEPTED_FLOWS = {"", "xtls-rprx-vision"}


def _recorder(monkeypatch):
    dispatched = []

    async def one(proto_user):
        dispatched.append(proto_user)

    async def many(proto_users):
        dispatched.extend(proto_users)

    monkeypatch.setattr(node_sync, "_dispatch_user_update", one)
    monkeypatch.setattr(node_sync, "_dispatch_users_update", many)
    return dispatched


async def _stored(username: str):
    async with TestSession() as session:
        return (await session.execute(select(User.id, User.hwid_limit).where(User.username == username))).first()


def _post_user(access_token, username, group_id, extra):
    body = {
        "username": username,
        "proxy_settings": {},
        "data_limit": 1024 * 1024,
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
        "group_ids": [group_id],
    }
    body.update(extra)
    try:
        response = client.post("/api/user", headers=auth_headers(access_token), json=body)
        return response.status_code, response.json()
    except ValueError as exc:
        return f"server crashed after commit: {exc}", None


async def test_a_vless_flow_xray_rejects_at_config_build_is_refused_by_the_api(access_token, monkeypatch):
    dispatched = _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("flow_group"))
    username = unique_name("flow_user")
    try:
        code, body = _post_user(access_token, username, group["id"], {"proxy_settings": {"vless": {"flow": "not-a-flow"}}})
        stored = await _stored(username)
        pushed_flows = {p.proxies.vless.flow for p in dispatched if stored and p.email == str(stored.id)}
        assert code in (400, 422), (
            f"API accepted vless.flow={body and body['proxy_settings']['vless'].get('flow')!r} (status {code}); "
            f"node received flows {pushed_flows}; xray-core infra/conf/vless.go rejects any client flow outside "
            f"{XRAY_SERVER_ACCEPTED_FLOWS} when the inbound config is rebuilt"
        )
    finally:
        client.delete(f"/api/user/{username}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


async def test_the_udp443_client_flow_is_accepted_but_reaches_the_node_as_vision(access_token, monkeypatch):
    dispatched = _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("udp443_group"))
    username = unique_name("udp443_user")
    try:
        code, body = _post_user(
            access_token, username, group["id"], {"proxy_settings": {"vless": {"flow": "xtls-rprx-vision-udp443"}}}
        )
        assert code == 201, body
        assert body["proxy_settings"]["vless"]["flow"] == "xtls-rprx-vision-udp443"
        pushed_flows = {p.proxies.vless.flow for p in dispatched if p.email == str(body["id"])}
        assert pushed_flows == {"xtls-rprx-vision"}, pushed_flows
    finally:
        client.delete(f"/api/user/{username}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_a_stored_unusable_flow_never_reaches_a_node():
    settings = ProxyTable().dict()
    settings["vless"]["flow"] = "not-a-flow"
    proto = _serialize_user_for_node(7, settings, ["vless-in"], None, None)
    assert proto.proxies.vless.flow in XRAY_SERVER_ACCEPTED_FLOWS


async def test_a_negative_hwid_limit_is_refused_before_it_is_committed(access_token, monkeypatch):
    dispatched = _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("hwid_group"))
    username = unique_name("neg_hwid")
    try:
        code, _ = _post_user(access_token, username, group["id"], {"hwid_limit": -1})
        stored = await _stored(username)
        delete_outcome = None
        if stored is not None:
            try:
                delete_outcome = client.delete(f"/api/user/{username}", headers=auth_headers(access_token)).status_code
            except ValueError as exc:
                delete_outcome = f"server crashed after the row was deleted: {exc}"
        after_delete = await _stored(username)
        removal_pushed = any(p.email == str(stored.id) and not p.inbounds for p in dispatched) if stored else None
        assert code in (400, 422) and stored is None, (
            f"create outcome={code!r}, row committed={stored!r}; delete outcome={delete_outcome!r}, "
            f"row still present after delete={after_delete is not None}, node removal pushed={removal_pushed}"
        )
    finally:
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


async def test_one_user_with_a_negative_hwid_limit_does_not_sink_the_whole_batch():
    good = SimpleNamespace(
        id=1, status=UserStatus.limited, proxy_settings=ProxyTable().dict(), hwid_limit=None, groups=[]
    )
    bad = SimpleNamespace(id=2, status=UserStatus.limited, proxy_settings=ProxyTable().dict(), hwid_limit=-1, groups=[])
    try:
        protos = await serialize_users_for_node([good, bad])
    except ValueError as exc:
        pytest.fail(f"serializing a batch that holds one negative hwid_limit raised {exc!r}: nobody in it is pushed")
    assert {p.email for p in protos} >= {"1"}


async def test_after_hwid_limit_turns_negative_a_disable_still_reaches_the_node(access_token, monkeypatch):
    dispatched = _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("hwid_chain_group"))
    username = unique_name("hwid_chain")
    code, body = _post_user(access_token, username, group["id"], {})
    assert code == 201
    user_id = body["id"]
    try:
        outcomes = {}
        for step, call in (
            ("modify", lambda: client.put(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token), json={"hwid_limit": -1})),
            ("disable", lambda: client.put(f"/api/user/by-id/{user_id}/disabled", headers=auth_headers(access_token), json={"disabled": True})),
        ):
            try:
                outcomes[step] = call().status_code
            except ValueError as exc:
                outcomes[step] = f"crashed after commit: {exc}"
        async with TestSession() as session:
            db_status = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()
        last_push = [p for p in dispatched if p.email == str(user_id)][-1]
        assert not list(last_push.inbounds), (
            f"DB status={db_status}, step outcomes={outcomes}, but the last state the node received still has "
            f"inbounds {list(last_push.inbounds)}: the disabled user keeps working"
        )
    finally:
        async with TestSession() as session:
            await session.execute(User.__table__.update().where(User.id == user_id).values(hwid_limit=None))
            await session.commit()
        client.delete(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def _put_user(access_token, user_id, body):
    return client.put(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token), json=body)


async def test_user_inputs_outside_what_the_nodes_accept_are_refused(access_token, monkeypatch):
    _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("bounds_group"))
    code, body = _post_user(access_token, unique_name("bounds"), group["id"], {})
    assert code == 201
    user_id = body["id"]
    try:
        for payload in (
            {"proxy_settings": {"vless": {"flow": "xtls-rprx-vision-udp"}}},
            {"proxy_settings": {"vless": {"flow": 5}}},
            {"proxy_settings": {"vless": {"flow": ["xtls-rprx-vision"]}}},
            {"proxy_settings": {"openvpn": {"password": "pw\n</auth-user-pass>\nscript-security 2"}}},
            {"proxy_settings": {"openvpn": {"password": 'pw"'}}},
            {"hwid_limit": -1},
            {"hwid_limit": 2**31},
            {"hwid_limit": 2**32},
            {"hwid_limit": 9_999_999_999},
            {"auto_delete_in_days": 36501},
            {"auto_delete_in_days": 99_999_999},
        ):
            response = _put_user(access_token, user_id, payload)
            assert response.status_code == 422, (payload, response.status_code)
        for payload in (
            {"proxy_settings": {"vless": {"flow": "xtls-rprx-vision"}}},
            {"proxy_settings": {"vless": {"flow": "xtls-rprx-vision-udp443"}}},
            {"proxy_settings": {"vless": {"flow": ""}}},
            {"proxy_settings": {"vless": {"flow": None}}},
            {"proxy_settings": {"openvpn": {"password": "Safe_pass-123"}}},
            {"hwid_limit": 0},
            {"hwid_limit": 2**31 - 1},
            {"hwid_limit": None},
            {"auto_delete_in_days": 36500},
            {"auto_delete_in_days": -1},
        ):
            response = _put_user(access_token, user_id, payload)
            assert response.status_code == 200, (payload, response.text)
    finally:
        client.delete(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_a_template_hwid_limit_outside_the_node_range_is_refused(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("tpl_bounds_group"))
    try:
        for hwid_limit, expected in ((-1, 422), (2**31, 422), (0, 201), (5, 201)):
            response = client.post(
                "/api/user_template",
                headers=auth_headers(access_token),
                json={"name": unique_name("tpl_bounds"), "group_ids": [group["id"]], "hwid_limit": hwid_limit},
            )
            assert response.status_code == expected, (hwid_limit, response.text)
            if response.status_code == 201:
                client.delete(f"/api/user_template/{response.json()['id']}", headers=auth_headers(access_token))
    finally:
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


async def test_a_stored_unusable_flow_still_reads_and_is_never_pushed(access_token, monkeypatch):
    dispatched = _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("stored_flow_group"))
    code, body = _post_user(access_token, unique_name("stored_flow"), group["id"], {})
    assert code == 201
    user_id = body["id"]
    try:
        async with TestSession() as session:
            stored = (await session.execute(select(User.proxy_settings).where(User.id == user_id))).scalar_one()
            stored = {**stored, "vless": {**stored["vless"], "flow": "xtls-rprx-vision-udp-443"}}
            await session.execute(User.__table__.update().where(User.id == user_id).values(proxy_settings=stored))
            await session.commit()
        response = client.get(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        assert response.status_code == 200
        assert response.json()["proxy_settings"]["vless"].get("flow") is None
        assert _put_user(access_token, user_id, {"note": "touch"}).status_code == 200
        pushed = [p for p in dispatched if p.email == str(user_id)]
        assert pushed and {p.proxies.vless.flow for p in pushed} <= XRAY_SERVER_ACCEPTED_FLOWS
    finally:
        client.delete(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_the_openvpn_connection_cap_always_fits_the_node_field():
    settings = ProxyTable().dict()
    settings["openvpn"]["password"] = "pw"
    for hwid_limit, expected in (
        (None, 0),
        (0, 0),
        (-5, 0),
        (3, 3),
        (2**32 - 1, 2**32 - 1),
        (2**32, 2**32 - 1),
        (9_999_999_999, 2**32 - 1),
        (2**40, 2**32 - 1),
    ):
        proto = _serialize_user_for_node(9, settings, ["ovpn-in"], None, hwid_limit)
        assert proto.proxies.open_vpn.max_concurrent_connections == expected, hwid_limit


async def test_a_user_that_cannot_be_serialized_is_revoked_while_the_rest_of_the_batch_is_pushed():
    good = SimpleNamespace(id=1, status=UserStatus.disabled, proxy_settings=ProxyTable().dict(), hwid_limit=None, groups=[])
    broken = SimpleNamespace(id=2, status=UserStatus.disabled, proxy_settings=None, hwid_limit=None, groups=[])
    protos = await serialize_users_for_node([good, broken])
    by_email = {p.email: p for p in protos}
    assert set(by_email) == {"1", "2"}
    assert not list(by_email["2"].inbounds)


def test_the_node_only_ever_receives_a_flow_both_cores_accept():
    settings = ProxyTable().dict()
    for stored, expected in (
        (None, ""),
        ("", ""),
        ("xtls-rprx-vision", "xtls-rprx-vision"),
        ("xtls-rprx-vision-udp443", "xtls-rprx-vision"),
        ("not-a-flow", ""),
        (7, ""),
        (["xtls-rprx-vision"], ""),
    ):
        settings["vless"]["flow"] = stored
        assert _serialize_user_for_node(3, settings, ["vless-in"], None, None).proxies.vless.flow == expected, stored
