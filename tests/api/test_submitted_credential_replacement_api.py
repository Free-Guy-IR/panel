import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import status
from pydantic import ValidationError
from sqlalchemy import select

from app.db.models import User
from tests.api import TestSession, client
from tests.api.helpers import (
    OPERATOR_ROLE_ID,
    auth_headers,
    create_admin,
    create_core,
    create_group,
    create_user,
    unique_name,
)

HELD_FIELDS = (("hysteria", "auth"), ("hysteria2", "password"), ("tuic", "uuid"), ("tuic", "password"))


async def _stored(username):
    async with TestSession() as db:
        return await db.scalar(select(User.proxy_settings).where(User.username == username))


def _stored_settings(username):
    return asyncio.run(_stored(username))


def _sales_bot_template():
    return {
        "vmess": {},
        "vless": {"flow": None},
        "trojan": {},
        "shadowsocks": {"method": "chacha20-ietf-poly1305"},
        "wireguard": {"private_key": None, "public_key": None, "peer_ips": []},
        "hysteria": {"auth": f"bot-auth-{uuid4().hex}"},
        "hysteria2": {"password": f"bot-hy2-{uuid4().hex}"},
        "tuic": {"uuid": str(uuid4()), "password": f"bot-tuic-{uuid4().hex}"},
        "openvpn": {"password": None},
        "l2tp": {"password": None},
    }


def _sales_bot_body(username, template, group_ids):
    return {
        "proxy_settings": template,
        "data_limit": 1073741824,
        "username": username,
        "note": "123456789 | someone | buy",
        "data_limit_reset_strategy": "no_reset",
        "group_ids": group_ids,
        "expire": (datetime.now(UTC) + timedelta(days=30)).replace(microsecond=0).isoformat(),
    }


def _secret(settings, protocol, attribute):
    return str((settings.get(protocol) or {}).get(attribute))


def test_a_sales_bot_template_holding_another_users_credentials_creates_every_user(access_token):
    create_core(access_token)
    group = create_group(access_token, name=unique_name("bot"))
    template = _sales_bot_template()
    names = [unique_name("bot"), unique_name("bot"), unique_name("bot")]

    responses = [
        client.post(
            "/api/user", headers=auth_headers(access_token), json=_sales_bot_body(name, template, [group["id"]])
        )
        for name in names
    ]

    assert [response.status_code for response in responses] == [status.HTTP_201_CREATED] * 3, [
        response.text for response in responses
    ]
    stored = [_stored_settings(name) for name in names]
    for protocol, attribute in HELD_FIELDS:
        values = [_secret(settings, protocol, attribute) for settings in stored]
        assert len(set(values)) == len(names), (protocol, attribute, values)
        assert values[0] == str(template[protocol][attribute])
        for response, settings in zip(responses, stored, strict=True):
            assert _secret(response.json()["proxy_settings"], protocol, attribute) == _secret(
                settings, protocol, attribute
            )


def test_a_modify_that_submits_a_credential_another_user_holds_gets_a_fresh_one(access_token):
    value = f"auth-{uuid4().hex}"
    create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})
    user = create_user(access_token)
    before = _stored_settings(user["username"])

    response = client.put(
        f"/api/user/{user['username']}",
        headers=auth_headers(access_token),
        json={"proxy_settings": {**user["proxy_settings"], "hysteria": {"auth": value}}},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    after = _stored_settings(user["username"])
    assert after["hysteria"]["auth"] not in (value, before["hysteria"]["auth"])
    assert response.json()["proxy_settings"]["hysteria"]["auth"] == after["hysteria"]["auth"]
    assert after["vless"] == before["vless"]


def test_a_credential_of_a_deleted_user_is_replaced_not_handed_on(access_token):
    value = f"auth-{uuid4().hex}"
    first = create_user(access_token, payload={"proxy_settings": {"hysteria2": {"password": value}}})
    assert client.delete(f"/api/user/{first['username']}", headers=auth_headers(access_token)).status_code == 204
    username = unique_name("heir")

    response = client.post(
        "/api/user",
        headers=auth_headers(access_token),
        json={"username": username, "proxy_settings": {"hysteria2": {"password": value}}, "status": "active"},
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert _stored_settings(username)["hysteria2"]["password"] != value


def test_a_revoked_credential_resubmitted_for_its_own_user_keeps_the_current_one(access_token):
    value = f"auth-{uuid4().hex}"
    user = create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})
    revoked = client.post(f"/api/user/{user['username']}/revoke_sub", headers=auth_headers(access_token)).json()
    current = revoked["proxy_settings"]["hysteria"]["auth"]

    response = client.put(
        f"/api/user/{user['username']}",
        headers=auth_headers(access_token),
        json={"proxy_settings": {**revoked["proxy_settings"], "hysteria": {"auth": value}}},
    )

    assert response.status_code == status.HTTP_200_OK, response.text
    assert current != value
    assert _stored_settings(user["username"])["hysteria"]["auth"] == current
    assert response.json()["proxy_settings"]["hysteria"]["auth"] == current


def test_strict_mode_still_refuses_a_revoked_credential_on_its_own_user(access_token, refuse_submitted_collisions):
    value = f"auth-{uuid4().hex}"
    user = create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})
    revoked = client.post(f"/api/user/{user['username']}/revoke_sub", headers=auth_headers(access_token)).json()

    response = client.put(
        f"/api/user/{user['username']}",
        headers=auth_headers(access_token),
        json={"proxy_settings": {**revoked["proxy_settings"], "hysteria": {"auth": value}}},
    )

    assert response.status_code == status.HTTP_409_CONFLICT, response.text
    assert _stored_settings(user["username"])["hysteria"]["auth"] == revoked["proxy_settings"]["hysteria"]["auth"]


def test_a_sales_bot_renewal_after_a_revoke_keeps_the_first_holders_current_credentials(access_token):
    create_core(access_token)
    group = create_group(access_token, name=unique_name("bot"))
    template = _sales_bot_template()
    username = unique_name("bot")
    created = client.post(
        "/api/user", headers=auth_headers(access_token), json=_sales_bot_body(username, template, [group["id"]])
    )
    assert created.status_code == status.HTTP_201_CREATED, created.text
    assert client.post(f"/api/user/{username}/revoke_sub", headers=auth_headers(access_token)).status_code == 200
    after_revoke = _stored_settings(username)

    renewals = [
        client.put(
            f"/api/user/{username}",
            headers=auth_headers(access_token),
            json={
                key: value
                for key, value in _sales_bot_body(username, template, [group["id"]]).items()
                if key != "username"
            },
        )
        for _ in range(2)
    ]

    assert [response.status_code for response in renewals] == [status.HTTP_200_OK] * 2, [
        response.text for response in renewals
    ]
    stored = _stored_settings(username)
    for protocol, attribute in HELD_FIELDS:
        assert _secret(after_revoke, protocol, attribute) != str(template[protocol][attribute]), (protocol, attribute)
        assert _secret(stored, protocol, attribute) == _secret(after_revoke, protocol, attribute), (protocol, attribute)
        assert _secret(renewals[-1].json()["proxy_settings"], protocol, attribute) == _secret(
            stored, protocol, attribute
        )


def test_an_operator_submitting_a_credential_of_a_user_it_cannot_see_learns_nothing_about_that_user(access_token):
    value = f"auth-{uuid4().hex}"
    victim = create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})
    operator = create_admin(access_token, role_id=OPERATOR_ROLE_ID)
    token = client.post(
        "/api/admin/token",
        data={"username": operator["username"], "password": operator["password"], "grant_type": "password"},
    ).json()["access_token"]
    username = unique_name("clone")

    response = client.post(
        "/api/user",
        headers=auth_headers(token),
        json={"username": username, "proxy_settings": {"hysteria": {"auth": value}}, "status": "active"},
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert victim["username"] not in response.text
    assert _stored_settings(username)["hysteria"]["auth"] != value
    assert _stored_settings(victim["username"])["hysteria"]["auth"] == value


def test_the_replacement_is_logged_the_way_production_logs_it(access_token, caplog):
    value = f"auth-{uuid4().hex}"
    create_user(access_token, payload={"proxy_settings": {"hysteria": {"auth": value}}})

    with caplog.at_level("WARNING"):
        response = client.post(
            "/api/user",
            headers=auth_headers(access_token),
            json={"username": unique_name("bot"), "proxy_settings": {"hysteria": {"auth": value}}, "status": "active"},
        )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    assert (
        "proxy secret hysteria.auth submitted for this user is already stored by 1 other user(s); "
        "replacing it with a freshly generated unique value"
    ) in caplog.text
    assert value not in caplog.text


def test_strict_mode_still_refuses_the_sales_bot_template(access_token, refuse_submitted_collisions):
    create_core(access_token)
    group_ids = [create_group(access_token, name=unique_name("bot"))["id"]]
    template = _sales_bot_template()
    first = client.post(
        "/api/user", headers=auth_headers(access_token), json=_sales_bot_body(unique_name("bot"), template, group_ids)
    )
    username = unique_name("bot")

    second = client.post(
        "/api/user", headers=auth_headers(access_token), json=_sales_bot_body(username, template, group_ids)
    )

    assert first.status_code == status.HTTP_201_CREATED, first.text
    assert second.status_code == status.HTTP_409_CONFLICT, second.text
    assert _stored_settings(username) is None


def test_the_default_matches_production_and_the_value_is_read_case_insensitively(monkeypatch):
    from config import ProxySecretSettings, SubmittedSecretCollision

    monkeypatch.delenv("PROXY_SECRET_SUBMITTED_COLLISION", raising=False)
    assert ProxySecretSettings(_env_file=None).submitted_collision is SubmittedSecretCollision.replace
    monkeypatch.setenv("PROXY_SECRET_SUBMITTED_COLLISION", " Refuse ")
    assert ProxySecretSettings(_env_file=None).submitted_collision is SubmittedSecretCollision.refuse
    monkeypatch.setenv("PROXY_SECRET_SUBMITTED_COLLISION", "ignore")
    with pytest.raises(ValidationError):
        ProxySecretSettings(_env_file=None)
