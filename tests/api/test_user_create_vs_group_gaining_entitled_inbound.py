import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import status
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import base
from app.fork.cores.openvpn import generate_openvpn_pki
from tests.api import DATABASE_URL, IS_SQLITE, app, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    create_user_template,
    delete_core,
    delete_group,
    delete_user_template,
    get_inbounds,
    unique_name,
)

REACH_SECONDS = 20
PROTOCOLS = ["l2tp", "openvpn"]
PATHS = ["create", "bulk_from_template"]
OPENVPN_PORTS = {"create": 11941, "bulk_from_template": 11942}


def _entitled_core(access_token, protocol, tag, path):
    if protocol == "l2tp":
        config = {
            "inbound_tag": tag,
            "server_addr": "203.0.113.10",
            "pool": "10.31.0.0/24",
            "psk": "correct-horse-battery",
        }
    else:
        config = {
            "instances": [{"tag": tag, "protocol": "udp", "port": OPENVPN_PORTS[path], "network": "10.38.0.0/24"}],
            "pki": generate_openvpn_pki(),
        }
    response = client.post(
        "/api/core",
        headers=auth_headers(access_token),
        json={
            "name": unique_name(f"race_{protocol}"),
            "type": protocol,
            "config": config,
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _created_usernames(path, created):
    return [created] if path == "create" else [f"{created}1", f"{created}2"]


def _passwords(access_token, usernames, protocol) -> dict[str, str | None]:
    found = {}
    for username in usernames:
        response = client.get(f"/api/user/{username}", headers=auth_headers(access_token))
        assert response.status_code == status.HTTP_200_OK, response.text
        found[username] = ((response.json().get("proxy_settings") or {}).get(protocol) or {}).get("password")
    return found


def _creation_request(path, created, shared, template):
    if path == "create":
        return "/api/user", {
            "username": created,
            "proxy_settings": {},
            "group_ids": [shared["id"]],
            "status": "active",
            "data_limit": 1024 * 1024,
            "data_limit_reset_strategy": "no_reset",
        }
    return "/api/users/bulk/from_template", {
        "user_template_id": template["id"],
        "count": 2,
        "strategy": "sequence",
        "username": created,
    }


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_a_user_created_while_its_group_gains_an_entitled_inbound_gets_its_password(
    access_token, monkeypatch, protocol, path
):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")
    from app.operation.user import UserOperation

    entitled_tag = unique_name(f"race{protocol}")
    xray_core = create_core(access_token)
    entitled_core = _entitled_core(access_token, protocol, entitled_tag, path)
    xray_tag = next(tag for tag in get_inbounds(access_token) if tag != entitled_tag)
    shared = create_group(access_token, name=unique_name("race_entitled_shared"), inbound_tags=[xray_tag])
    template = create_user_template(access_token, group_ids=[shared["id"]]) if path == "bulk_from_template" else None
    created = unique_name("race_entitled_new")
    after = unique_name("race_entitled_after")
    racing_engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {}
    )
    racing_session = async_sessionmaker(racing_engine, autoflush=False, expire_on_commit=False)

    async def racing_db():
        async with racing_session() as db:
            yield db

    real_validate = UserOperation.validate_all_groups
    gate = {"loaded": None, "edited": None, "held": False}

    async def validate_then_wait_for_the_edit(self, db, model, admin=None, **kwargs):
        groups = await real_validate(self, db, model, admin, **kwargs)
        if str(getattr(model, "username", "") or "").startswith(created) and not gate["held"]:
            gate["held"] = True
            gate["loaded"].set()
            await asyncio.wait_for(gate["edited"].wait(), REACH_SECONDS)
        return groups

    monkeypatch.setattr(UserOperation, "validate_all_groups", validate_then_wait_for_the_edit)
    monkeypatch.setattr("app.operation.group.sync_users", AsyncMock())
    headers = auth_headers(access_token)
    url, body = _creation_request(path, created, shared, template)

    async def run():
        gate["loaded"], gate["edited"] = asyncio.Event(), asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as racing_client:
            creating = asyncio.create_task(racing_client.post(url, headers=headers, json=body))
            await asyncio.wait_for(gate["loaded"].wait(), REACH_SECONDS)
            edited = await racing_client.put(
                f"/api/group/{shared['id']}",
                headers=headers,
                json={"name": shared["name"], "inbound_tags": [xray_tag, entitled_tag]},
            )
            gate["edited"].set()
            return edited, await creating

    previous_get_db = app.dependency_overrides[base.get_db]
    app.dependency_overrides[base.get_db] = racing_db
    try:
        edited, creation = asyncio.run(run())
    finally:
        app.dependency_overrides[base.get_db] = previous_get_db
        monkeypatch.setattr(UserOperation, "validate_all_groups", real_validate)
        asyncio.run(racing_engine.dispose())
    try:
        assert edited.status_code == status.HTTP_200_OK, edited.text
        assert creation.status_code == status.HTTP_201_CREATED, creation.text
        create_user(access_token, group_ids=[shared["id"]], payload={"username": after})
        raced = _passwords(access_token, _created_usernames(path, created), protocol)
        control = _passwords(access_token, [after], protocol)
        assert all(control.values()), f"a user created into the group after the edit must hold a {protocol} password"
        assert all(raced.values()), (
            f"users created while their group gained {protocol} are in the group but hold no {protocol} password: "
            f"{raced}"
        )
    finally:
        for username in [*_created_usernames(path, created), after]:
            client.delete(f"/api/user/{username}", headers=headers)
        if template is not None:
            delete_user_template(access_token, template["id"])
        delete_group(access_token, shared["id"])
        delete_core(access_token, entitled_core["id"])
        delete_core(access_token, xray_core["id"])
