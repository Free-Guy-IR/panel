from sqlalchemy import select

from app.db.models import User
from app.node import sync as node_sync
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    create_group,
    create_user,
    delete_admin,
    delete_core,
    delete_group,
    unique_name,
)


def _recorder(monkeypatch):
    dispatched = []

    async def one(proto_user):
        dispatched.append(proto_user)

    async def many(proto_users):
        dispatched.extend(proto_users)

    monkeypatch.setattr(node_sync, "_dispatch_user_update", one)
    monkeypatch.setattr(node_sync, "_dispatch_users_update", many)
    return dispatched


def test_seeded_operator_cannot_arm_an_invalid_vless_flow(access_token, monkeypatch):
    dispatched = _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("va4a_flow"))
    operator = create_admin(access_token, role_id=3)
    token = client.post(
        "/api/admin/token",
        data={"username": operator["username"], "password": operator["password"], "grant_type": "password"},
    ).json()["access_token"]
    created = client.post(
        "/api/user",
        headers=auth_headers(token),
        json={
            "username": unique_name("va4a_op"),
            "proxy_settings": {},
            "data_limit": 1 << 30,
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
            "group_ids": [group["id"]],
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]
    try:
        statuses = {}
        for flow in ("xtls-rprx-vision-udp443", "not-a-flow"):
            statuses[flow] = client.put(
                f"/api/user/by-id/{user_id}",
                headers=auth_headers(token),
                json={"proxy_settings": {"vless": {"flow": flow}}},
            ).status_code
        flows = {p.proxies.vless.flow for p in dispatched if p.email == str(user_id)}
        assert statuses["not-a-flow"] in (400, 422) and flows <= {"", "xtls-rprx-vision"}, (statuses, flows)
    finally:
        client.delete(f"/api/user/by-id/{user_id}", headers=auth_headers(access_token))
        delete_admin(access_token, operator["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


async def test_hwid_limit_above_uint32_is_refused_or_harmless(access_token, monkeypatch):
    dispatched = _recorder(monkeypatch)
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("va4a_big"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("va4a_big")})
    try:
        outcomes = {}
        for step, body, path in (
            ("modify", {"hwid_limit": 9999999999}, f"/api/user/by-id/{user['id']}"),
            ("disable", {"disabled": True}, f"/api/user/by-id/{user['id']}/disabled"),
        ):
            try:
                outcomes[step] = client.put(path, headers=auth_headers(access_token), json=body).status_code
            except ValueError as exc:
                outcomes[step] = f"crashed after commit: {exc}"
        async with TestSession() as session:
            row = (await session.execute(select(User.status, User.hwid_limit).where(User.id == user["id"]))).first()
        last = [p for p in dispatched if p.email == str(user["id"])][-1]
        assert not list(last.inbounds) and all(isinstance(v, int) for v in outcomes.values()), (
            f"db row={tuple(row)}, outcomes={outcomes}, node still has {len(last.inbounds)} inbounds"
        )
    finally:
        async with TestSession() as session:
            await session.execute(User.__table__.update().where(User.id == user["id"]).values(hwid_limit=None))
            await session.commit()
        client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
