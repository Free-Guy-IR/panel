import asyncio
from uuid import UUID, uuid4

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.models import User
from app.node.user import core_users
from tests.api import TestSession, client
from tests.api.helpers import (
    OPERATOR_ROLE_ID,
    auth_headers,
    create_admin,
    create_core,
    create_group,
    create_hosts_for_inbounds,
    create_user,
    unique_name,
)
from tests.api.sample_data import XRAY_CONFIG

HYSTERIA_TAGS = [inbound["tag"] for inbound in XRAY_CONFIG["inbounds"] if inbound.get("protocol") == "hysteria"]


def _post_user(access_token, username, proxy_settings, group_ids=None):
    body = {
        "username": username,
        "proxy_settings": proxy_settings,
        "data_limit": 1024 * 1024,
        "data_limit_reset_strategy": "no_reset",
        "status": "active",
    }
    if group_ids is not None:
        body["group_ids"] = list(group_ids)
    return client.post("/api/user", headers=auth_headers(access_token), json=body)


def _put_user(access_token, username, proxy_settings):
    return client.put(
        f"/api/user/{username}", headers=auth_headers(access_token), json={"proxy_settings": proxy_settings}
    )


async def _stored(username):
    async with TestSession() as db:
        return await db.scalar(select(User.proxy_settings).where(User.username == username))


def _stored_settings(username):
    return asyncio.run(_stored(username))


def _fresh_auth():
    return f"auth-{uuid4().hex}"


def _variant_forms(value: str) -> list[str]:
    parsed = UUID(value)
    route_bytes_changed = bytearray(parsed.bytes)
    route_bytes_changed[6] ^= 0x0F
    route_bytes_changed[7] ^= 0xF0
    return [
        value.upper(),
        parsed.hex,
        "{" + value + "}",
        "urn:uuid:" + value,
        str(UUID(bytes=bytes(route_bytes_changed))),
    ]


@pytest.mark.parametrize(
    ("protocol", "attribute"),
    [("hysteria", "auth"), ("hysteria2", "password"), ("trojan", "password"), ("tuic", "password")],
)
def test_a_create_that_submits_a_credential_another_user_holds_is_refused(access_token, protocol, attribute):
    value = _fresh_auth() + "-long-enough-for-every-kind"
    holder = create_user(access_token, payload={"proxy_settings": {protocol: {attribute: value}}})
    assert holder["proxy_settings"][protocol][attribute] == value
    username = unique_name("copycat")

    response = _post_user(access_token, username, {protocol: {attribute: value}})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert _stored_settings(username) is None


def test_a_modify_that_submits_a_credential_another_user_holds_is_refused(access_token):
    value = _fresh_auth()
    create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})
    victim = create_user(access_token)
    before = _stored_settings(victim["username"])

    response = _put_user(access_token, victim["username"], {**victim["proxy_settings"], "hysteria": {"auth": value}})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert _stored_settings(victim["username"])["hysteria"] == before["hysteria"]


def test_a_modify_that_resubmits_the_users_own_credential_is_accepted(access_token):
    value = _fresh_auth()
    user = create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})

    response = _put_user(access_token, user["username"], user["proxy_settings"])

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["proxy_settings"]["hysteria"]["auth"] == value


def test_a_credential_retired_by_a_revoke_is_never_handed_to_another_user(access_token):
    value = _fresh_auth()
    first = create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})
    revoked = client.post(f"/api/user/{first['username']}/revoke_sub", headers=auth_headers(access_token))
    assert revoked.status_code == status.HTTP_200_OK, revoked.text
    assert revoked.json()["proxy_settings"]["hysteria"]["auth"] != value
    username = unique_name("heir")

    response = _post_user(access_token, username, {"hysteria": {"auth": value}})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert _stored_settings(username) is None


def test_a_credential_of_a_deleted_user_is_never_handed_to_another_user(access_token):
    value = _fresh_auth()
    first = create_user(access_token, payload={"proxy_settings": {"hysteria2": {"password": value}}})
    deleted = client.delete(f"/api/user/{first['username']}", headers=auth_headers(access_token))
    assert deleted.status_code == status.HTTP_204_NO_CONTENT, deleted.text
    username = unique_name("heir")

    response = _post_user(access_token, username, {"hysteria2": {"password": value}})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text


def test_a_revoked_credential_cannot_be_put_back_on_its_own_user(access_token):
    value = _fresh_auth()
    user = create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})
    revoked = client.post(f"/api/user/{user['username']}/revoke_sub", headers=auth_headers(access_token)).json()

    response = _put_user(access_token, user["username"], {**revoked["proxy_settings"], "hysteria": {"auth": value}})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text


@pytest.mark.parametrize("form", range(5))
def test_uuid_shaped_hysteria_auths_that_xray_matches_as_one_are_refused(access_token, form):
    value = str(uuid4())
    create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})
    username = unique_name("lookalike")

    response = _post_user(access_token, username, {"hysteria": {"auth": _variant_forms(value)[form]}})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text


def test_vless_ids_that_differ_only_in_the_route_bytes_are_refused(access_token):
    value = str(uuid4())
    create_user(access_token, payload={"proxy_settings": {"vless": {"id": value}}})
    username = unique_name("lookalike")

    response = _post_user(access_token, username, {"vless": {"id": _variant_forms(value)[4]}})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text


def test_nodes_never_receive_an_empty_hysteria_credential(access_token):
    create_core(access_token)
    group = create_group(access_token)
    user = create_user(access_token, group_ids=[group["id"]])

    async def write_settings(settings):
        async with TestSession() as db:
            db_user = await db.scalar(select(User).where(User.id == user["id"]))
            db_user.proxy_settings = settings
            await db.commit()

    original = _stored_settings(user["username"])
    blanked = dict(original)
    blanked.pop("hysteria", None)
    blanked["hysteria2"] = {"password": ""}
    asyncio.run(write_settings(blanked))

    async def pushed():
        async with TestSession() as db:
            return [proto for proto in await core_users(db) if int(proto.email) == user["id"]]

    try:
        protos = asyncio.run(pushed())
    finally:
        asyncio.run(write_settings({**original, "hysteria": {"auth": _fresh_auth()}, "hysteria2": {"password": _fresh_auth()}}))
    assert protos
    for proto in protos:
        assert not proto.proxies.HasField("hysteria"), proto.proxies
        assert not proto.proxies.HasField("hysteria2"), proto.proxies
        assert proto.proxies.HasField("vless")


def _login(username, password):
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()["access_token"]


def test_an_operator_cannot_clone_a_credential_of_a_user_it_cannot_see(access_token):
    value = _fresh_auth()
    victim = create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})
    operator = create_admin(access_token, role_id=OPERATOR_ROLE_ID)
    operator_token = _login(operator["username"], operator["password"])
    assert client.get(f"/api/user/{victim['username']}", headers=auth_headers(operator_token)).status_code == 404
    username = unique_name("clone")

    response = _post_user(operator_token, username, {"hysteria": {"auth": value}})

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert victim["username"] not in response.text
    assert str(victim["id"]) not in response.json()["detail"]
    assert _stored_settings(username) is None


def test_links_served_right_after_a_credential_change_carry_the_new_credential(access_token):
    create_core(access_token)
    hosts = create_hosts_for_inbounds(access_token)
    group = create_group(access_token)
    user = create_user(access_token, group_ids=[group["id"]])
    old_auth = user["proxy_settings"]["hysteria"]["auth"]
    try:
        before = client.get(f"{user['subscription_url']}/links")
        assert before.status_code == status.HTTP_200_OK
        assert old_auth in before.text
        new_auth = _fresh_auth()

        changed = _put_user(access_token, user["username"], {**user["proxy_settings"], "hysteria": {"auth": new_auth}})
        assert changed.status_code == status.HTTP_200_OK, changed.text

        after = client.get(f"{changed.json()['subscription_url']}/links")
        assert after.status_code == status.HTTP_200_OK
        assert old_auth not in after.text
        assert new_auth in after.text
    finally:
        for host in hosts:
            client.delete(f"/api/host/{host['id']}", headers=auth_headers(access_token))
