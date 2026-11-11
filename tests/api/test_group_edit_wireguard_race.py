import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import base
from app.db.models import User
from app.fork.proxy_secrets import resync_markers
from app.utils.crypto import generate_wireguard_keypair
from tests.api import DATABASE_URL, IS_SQLITE, app, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    get_inbounds,
    unique_name,
)

HOLD_SECONDS = 0.5
REACH_THE_LOCK_SECONDS = 20
SUBNETS = {"modify": "10.47.1", "bulk_add": "10.47.2", "bulk_enable": "10.47.3"}


def _wg_core(access_token, interface_name, address):
    private_key, _ = generate_wireguard_keypair()
    return create_core(
        access_token,
        name=unique_name("wg_race_core"),
        config={"interface_name": interface_name, "private_key": private_key, "listen_port": 51820, "address": address},
        type="wg",
        fallbacks=[],
    )


def _group(access_token, group_id):
    response = client.get(f"/api/group/{group_id}", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK
    return response.json()


def _user(access_token, username):
    response = client.get(f"/api/user/{username}", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK
    return response.json()


def _usage(access_token, subnet):
    response = client.get("/api/wireguard/subnets", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK
    return next(row for row in response.json() if row["subnet"] == subnet)


def _race(monkeypatch, access_token, first, second):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")
    racing_engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {}
    )
    racing_session = async_sessionmaker(racing_engine, autoflush=False, expire_on_commit=False)

    async def racing_db():
        async with racing_session() as db:
            yield db

    real_mark_users = resync_markers.mark_users
    held = {"done": False, "event": None}

    async def mark_users_and_hold_the_lock(db, user_ids):
        await real_mark_users(db, user_ids)
        if not held["done"]:
            held["done"] = True
            held["event"].set()
            await asyncio.sleep(HOLD_SECONDS)

    monkeypatch.setattr(resync_markers, "mark_users", mark_users_and_hold_the_lock)
    headers = auth_headers(access_token)

    async def run():
        held["event"] = asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as racing_client:
            leading = asyncio.create_task(first(racing_client, headers))
            await asyncio.wait_for(held["event"].wait(), REACH_THE_LOCK_SECONDS)
            trailing = asyncio.create_task(second(racing_client, headers))
            return await asyncio.gather(leading, trailing)

    previous_get_db = app.dependency_overrides[base.get_db]
    app.dependency_overrides[base.get_db] = racing_db
    try:
        return asyncio.run(run())
    finally:
        app.dependency_overrides[base.get_db] = previous_get_db
        monkeypatch.setattr(resync_markers, "mark_users", real_mark_users)
        asyncio.run(racing_engine.dispose())


def _race_around_reconcile(monkeypatch, access_token, first, others):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")
    racing_engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {}
    )
    racing_session = async_sessionmaker(racing_engine, autoflush=False, expire_on_commit=False)

    async def racing_db():
        async with racing_session() as db:
            yield db

    from app.operation import core as core_module

    real_reconcile = core_module.reconcile_wireguard_subnets
    held = {"done": False, "event": None}

    async def hold_then_reconcile(db):
        if not held["done"]:
            held["done"] = True
            held["event"].set()
            await asyncio.sleep(HOLD_SECONDS)
        return await real_reconcile(db)

    monkeypatch.setattr(core_module, "reconcile_wireguard_subnets", hold_then_reconcile)
    headers = auth_headers(access_token)

    async def run():
        held["event"] = asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as racing_client:
            leading = asyncio.create_task(first(racing_client, headers))
            await asyncio.wait_for(held["event"].wait(), REACH_THE_LOCK_SECONDS)
            trailing = [asyncio.create_task(other(racing_client, headers)) for other in others]
            return await asyncio.gather(leading, *trailing)

    previous_get_db = app.dependency_overrides[base.get_db]
    app.dependency_overrides[base.get_db] = racing_db
    try:
        return asyncio.run(run())
    finally:
        app.dependency_overrides[base.get_db] = previous_get_db
        monkeypatch.setattr(core_module, "reconcile_wireguard_subnets", real_reconcile)
        asyncio.run(racing_engine.dispose())


def _peer_ips_by_user(rows, usernames):
    return {
        name: list(((settings or {}).get("wireguard") or {}).get("peer_ips") or [])
        for name, settings in rows
        if name in usernames
    }


def _pushed_user_ids(sync_mock):
    return sorted({user.id for call in sync_mock.await_args_list for user in call.args[0]})


@pytest.mark.parametrize("action", ["modify", "bulk_add", "bulk_enable"])
def test_two_group_edits_racing_for_the_last_wireguard_address_leave_the_loser_unchanged(
    access_token, monkeypatch, action
):
    prefix = SUBNETS[action]
    subnet = f"{prefix}.0/30"
    xray_core = create_core(access_token)
    interface_name = unique_name("wg_race")
    wg_core = _wg_core(access_token, interface_name, [f"{prefix}.1/30"])
    xray_tag = next(tag for tag in get_inbounds(access_token) if tag != interface_name)
    plain = create_group(access_token, name=unique_name("race_plain"), inbound_tags=[xray_tag])
    groups = [plain]
    users = []
    try:
        if action == "modify":
            sides = [
                create_group(access_token, name=unique_name("race_side"), inbound_tags=[xray_tag]) for _ in range(2)
            ]
            groups += sides
            users = [
                create_user(access_token, group_ids=[side["id"]], payload={"username": unique_name("race_u")})
                for side in sides
            ]
        elif action == "bulk_add":
            sides = [create_group(access_token, name=unique_name("race_wg"), inbound_tags=[interface_name])]
            groups += sides
            users = [
                create_user(access_token, group_ids=[plain["id"]], payload={"username": unique_name("race_u")})
                for _ in range(2)
            ]
        else:
            sides = [
                create_group(access_token, name=unique_name("race_wg"), inbound_tags=[interface_name]) for _ in range(2)
            ]
            groups += sides
            response = client.post(
                "/api/groups/bulk/disable",
                headers=auth_headers(access_token),
                json={"ids": [side["id"] for side in sides]},
            )
            assert response.status_code == status.HTTP_200_OK
            users = [
                create_user(
                    access_token, group_ids=[plain["id"], side["id"]], payload={"username": unique_name("race_u")}
                )
                for side in sides
            ]
        assert all(user["proxy_settings"]["wireguard"]["peer_ips"] == [] for user in users)

        def edit(index):
            async def send(racing_client, headers):
                if action == "modify":
                    side = sides[index]
                    return await racing_client.put(
                        f"/api/group/{side['id']}",
                        headers=headers,
                        json={"name": side["name"], "inbound_tags": [xray_tag, interface_name]},
                    )
                if action == "bulk_add":
                    return await racing_client.post(
                        "/api/groups/bulk/add",
                        headers=headers,
                        json={"group_ids": [sides[0]["id"]], "users": [users[index]["id"]]},
                    )
                return await racing_client.post(
                    "/api/groups/bulk/enable", headers=headers, json={"ids": [sides[index]["id"]]}
                )

            return send

        sync_mock = AsyncMock()
        monkeypatch.setattr("app.operation.group.sync_users", sync_mock)
        responses = _race(monkeypatch, access_token, edit(0), edit(1))

        codes = [response.status_code for response in responses]
        assert sorted(codes) == [status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST], [r.text for r in responses]
        winner = codes.index(status.HTTP_200_OK)
        loser = 1 - winner
        assert "no free addresses" in responses[loser].json()["detail"]

        stored_users = [_user(access_token, user["username"]) for user in users]
        if action == "modify":
            assert sorted(_group(access_token, sides[winner]["id"])["inbound_tags"]) == sorted(
                [xray_tag, interface_name]
            )
            assert _group(access_token, sides[loser]["id"])["inbound_tags"] == [xray_tag]
        elif action == "bulk_add":
            assert sides[0]["id"] in stored_users[winner]["group_ids"]
            assert stored_users[loser]["group_ids"] == [plain["id"]]
        else:
            assert _group(access_token, sides[winner]["id"])["is_disabled"] is False
            assert _group(access_token, sides[loser]["id"])["is_disabled"] is True
        assert stored_users[winner]["proxy_settings"]["wireguard"]["peer_ips"] == [f"{prefix}.2/32"]
        assert stored_users[loser]["proxy_settings"]["wireguard"]["peer_ips"] == []
        usage = _usage(access_token, subnet)
        assert (usage["used"], usage["free"]) == (1, 0)
        assert _pushed_user_ids(sync_mock) == [users[winner]["id"]]
    finally:
        for user in users:
            delete_user(access_token, user["username"])
        for group in reversed(groups):
            delete_group(access_token, group["id"])
        delete_core(access_token, wg_core["id"])
        delete_core(access_token, xray_core["id"])


def test_a_group_edit_waiting_for_the_lock_keeps_the_address_the_edit_before_it_gave(access_token, monkeypatch):
    prefix = "10.47.4"
    subnet = f"{prefix}.0/29"
    xray_core = create_core(access_token)
    interface_name = unique_name("wg_race")
    wg_core = _wg_core(access_token, interface_name, [f"{prefix}.1/29"])
    xray_tag = next(tag for tag in get_inbounds(access_token) if tag != interface_name)
    wg_group = create_group(access_token, name=unique_name("race_wg"), inbound_tags=[interface_name])
    shared = create_group(access_token, name=unique_name("race_shared"), inbound_tags=[xray_tag])
    user = create_user(access_token, group_ids=[shared["id"]], payload={"username": unique_name("race_u")})
    renamed = unique_name("race_renamed")
    try:

        async def give_the_address(racing_client, headers):
            return await racing_client.post(
                "/api/groups/bulk/add",
                headers=headers,
                json={"group_ids": [wg_group["id"]], "users": [user["id"]]},
            )

        async def edit_the_other_group(racing_client, headers):
            return await racing_client.put(
                f"/api/group/{shared['id']}",
                headers=headers,
                json={"name": renamed, "inbound_tags": [xray_tag]},
            )

        sync_mock = AsyncMock()
        monkeypatch.setattr("app.operation.group.sync_users", sync_mock)
        responses = _race(monkeypatch, access_token, give_the_address, edit_the_other_group)

        assert [response.status_code for response in responses] == [status.HTTP_200_OK, status.HTTP_200_OK], [
            response.text for response in responses
        ]
        stored = _user(access_token, user["username"])
        assert sorted(stored["group_ids"]) == sorted([wg_group["id"], shared["id"]])
        assert stored["proxy_settings"]["wireguard"]["peer_ips"] == [f"{prefix}.2/32"]
        assert _group(access_token, shared["id"])["name"] == renamed
        usage = _usage(access_token, subnet)
        assert usage["used"] == 1
        assert f"{prefix}.2" not in usage["free_ips"]
    finally:
        delete_user(access_token, user["username"])
        delete_group(access_token, shared["id"])
        delete_group(access_token, wg_group["id"])
        delete_core(access_token, wg_core["id"])
        delete_core(access_token, xray_core["id"])


def test_a_usage_write_on_a_member_during_a_group_edit_waits_for_it_instead_of_deadlocking(access_token, monkeypatch):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")
    prefix = "10.47.5"
    xray_core = create_core(access_token)
    interface_name = unique_name("wg_race")
    wg_core = _wg_core(access_token, interface_name, [f"{prefix}.1/29"])
    xray_tag = next(tag for tag in get_inbounds(access_token) if tag != interface_name)
    shared = create_group(access_token, name=unique_name("race_shared"), inbound_tags=[xray_tag])
    user = create_user(access_token, group_ids=[shared["id"]], payload={"username": unique_name("race_u")})
    racing_engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {}
    )
    racing_session = async_sessionmaker(racing_engine, autoflush=False, expire_on_commit=False)
    real_mark_users = resync_markers.mark_users
    writes = {"task": None}

    async def write_usage():
        async with racing_session() as db:
            await db.execute(update(User).where(User.id == user["id"]).values(used_traffic=User.used_traffic + 1))
            await db.commit()

    async def mark_users_then_meet_a_usage_write(db, user_ids):
        await real_mark_users(db, user_ids)
        if writes["task"] is None and user["id"] in list(user_ids):
            writes["task"] = asyncio.create_task(write_usage())
            await asyncio.sleep(HOLD_SECONDS)

    async def racing_db():
        async with racing_session() as db:
            yield db

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as racing_client:
            response = await racing_client.put(
                f"/api/group/{shared['id']}",
                headers=auth_headers(access_token),
                json={"name": shared["name"], "inbound_tags": [xray_tag, interface_name]},
            )
            outcome = (await asyncio.gather(writes["task"], return_exceptions=True))[0]
            return response, outcome

    async def stored_usage():
        async with racing_session() as db:
            return await db.scalar(select(User.used_traffic).where(User.id == user["id"]))

    monkeypatch.setattr("app.operation.group.sync_users", AsyncMock())
    monkeypatch.setattr(resync_markers, "mark_users", mark_users_then_meet_a_usage_write)
    previous_get_db = app.dependency_overrides[base.get_db]
    app.dependency_overrides[base.get_db] = racing_db
    try:
        before = asyncio.run(stored_usage())
        response, outcome = asyncio.run(run())
        after = asyncio.run(stored_usage())
    finally:
        app.dependency_overrides[base.get_db] = previous_get_db
        monkeypatch.setattr(resync_markers, "mark_users", real_mark_users)
        asyncio.run(racing_engine.dispose())

    try:
        assert response.status_code == status.HTTP_200_OK, response.text
        assert outcome is None, f"the usage write failed: {outcome!r}"
        assert after == before + 1
        assert _user(access_token, user["username"])["proxy_settings"]["wireguard"]["peer_ips"] == [f"{prefix}.2/32"]
    finally:
        delete_user(access_token, user["username"])
        delete_group(access_token, shared["id"])
        delete_core(access_token, wg_core["id"])
        delete_core(access_token, xray_core["id"])


def _member_world(access_token, prefix, mask):
    xray_core = create_core(access_token)
    interface_name = unique_name("wg_race")
    wg_core = _wg_core(access_token, interface_name, [f"{prefix}.1/{mask}"])
    xray_tag = next(tag for tag in get_inbounds(access_token) if tag != interface_name)
    shared = create_group(access_token, name=unique_name("race_shared"), inbound_tags=[xray_tag])
    member = create_user(access_token, group_ids=[shared["id"]], payload={"username": unique_name("race_u")})
    return SimpleNamespace(
        xray_core=xray_core,
        wg_core=wg_core,
        interface_name=interface_name,
        xray_tag=xray_tag,
        shared=shared,
        member=member,
    )


def _drop_member_world(access_token, world, usernames=()):
    for username in [world.member["username"], *usernames]:
        delete_user(access_token, username)
    delete_group(access_token, world.shared["id"])
    delete_core(access_token, world.wg_core["id"])
    delete_core(access_token, world.xray_core["id"])


def _give_the_group_wireguard(world):
    async def send(racing_client, headers):
        return await racing_client.put(
            f"/api/group/{world.shared['id']}",
            headers=headers,
            json={"name": world.shared["name"], "inbound_tags": [world.xray_tag, world.interface_name]},
        )

    return send


def test_a_member_modify_waiting_for_a_group_edit_keeps_the_address_the_edit_gave(access_token, monkeypatch):
    prefix = "10.47.6"
    world = _member_world(access_token, prefix, 29)
    try:

        async def modify_the_member(racing_client, headers):
            return await racing_client.put(
                f"/api/user/{world.member['username']}",
                headers=headers,
                json={"group_ids": [world.shared["id"]], "note": "edited while the group gained wireguard"},
            )

        monkeypatch.setattr("app.operation.group.sync_users", AsyncMock())
        responses = _race(monkeypatch, access_token, _give_the_group_wireguard(world), modify_the_member)

        assert [response.status_code for response in responses] == [status.HTTP_200_OK, status.HTTP_200_OK], [
            response.text for response in responses
        ]
        stored = _user(access_token, world.member["username"])
        assert stored["note"] == "edited while the group gained wireguard"
        assert stored["proxy_settings"]["wireguard"]["peer_ips"] == [f"{prefix}.2/32"]
        assert _usage(access_token, f"{prefix}.0/29")["used"] == 1
    finally:
        _drop_member_world(access_token, world)


def test_a_user_created_into_a_group_while_it_gains_wireguard_gets_an_address(access_token, monkeypatch):
    prefix = "10.47.7"
    world = _member_world(access_token, prefix, 29)
    created = unique_name("race_new")
    try:

        async def create_a_member(racing_client, headers):
            return await racing_client.post(
                "/api/user",
                headers=headers,
                json={
                    "username": created,
                    "proxy_settings": {},
                    "group_ids": [world.shared["id"]],
                    "status": "active",
                    "data_limit": 1024 * 1024,
                    "data_limit_reset_strategy": "no_reset",
                },
            )

        monkeypatch.setattr("app.operation.group.sync_users", AsyncMock())
        responses = _race(monkeypatch, access_token, _give_the_group_wireguard(world), create_a_member)

        assert [response.status_code for response in responses] == [status.HTTP_200_OK, status.HTTP_201_CREATED], [
            response.text for response in responses
        ]
        addresses = [
            _user(access_token, username)["proxy_settings"]["wireguard"]["peer_ips"]
            for username in (world.member["username"], created)
        ]
        assert all(len(peer_ips) == 1 for peer_ips in addresses), addresses
        assert addresses[0] != addresses[1]
        assert _usage(access_token, f"{prefix}.0/29")["used"] == 2
    finally:
        _drop_member_world(access_token, world, usernames=[created])


@pytest.mark.parametrize("mask", ["28", "29"], ids=["resized", "same_subnet"])
def test_a_wireguard_core_save_during_a_group_edit_lets_both_finish(access_token, monkeypatch, mask):
    prefix = "10.47.8" if mask == "28" else "10.47.12"
    world = _member_world(access_token, prefix, 29)
    try:

        async def save_the_core(racing_client, headers):
            return await racing_client.put(
                f"/api/core/{world.wg_core['id']}",
                headers=headers,
                params={"restart_nodes": False},
                json={
                    "name": world.wg_core["name"],
                    "type": "wg",
                    "config": {**world.wg_core["config"], "listen_port": 51821, "address": [f"{prefix}.1/{mask}"]},
                    "exclude_inbound_tags": [],
                    "fallbacks_inbound_tags": [],
                },
            )

        monkeypatch.setattr("app.operation.group.sync_users", AsyncMock())
        responses = _race(monkeypatch, access_token, _give_the_group_wireguard(world), save_the_core)

        assert [response.status_code for response in responses] == [status.HTTP_200_OK, status.HTTP_200_OK], [
            response.text for response in responses
        ]
        peer_ips = _user(access_token, world.member["username"])["proxy_settings"]["wireguard"]["peer_ips"]
        assert len(peer_ips) == 1
        assert peer_ips[0].startswith(f"{prefix}.")
        assert _usage(access_token, f"{prefix}.0/{mask}")["used"] == 1
    finally:
        _drop_member_world(access_token, world)


def _resize(world, prefix, mask):
    async def send(racing_client, headers):
        return await racing_client.put(
            f"/api/core/{world.wg_core['id']}",
            headers=headers,
            params={"restart_nodes": False},
            json={
                "name": world.wg_core["name"],
                "type": "wg",
                "config": {**world.wg_core["config"], "address": [f"{prefix}.1/{mask}"]},
                "exclude_inbound_tags": [],
                "fallbacks_inbound_tags": [],
            },
        )

    return send


def test_a_core_resize_overlapping_a_group_edit_never_hands_out_a_held_address(access_token, monkeypatch):
    prefix = "10.47.9"
    world = _member_world(access_token, prefix, 29)
    wg_group = create_group(access_token, name=unique_name("race_wg"), inbound_tags=[world.interface_name])
    holder = create_user(access_token, group_ids=[wg_group["id"]], payload={"username": unique_name("race_h")})
    assert holder["proxy_settings"]["wireguard"]["peer_ips"] == [f"{prefix}.2/32"]
    snapshot_engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {}
    )
    snapshot_session = async_sessionmaker(snapshot_engine, autoflush=False, expire_on_commit=False)
    from app.operation import core as core_module

    real_reconcile = core_module.reconcile_wireguard_subnets
    committed_at_reconcile = []

    async def note_then_reconcile(db):
        async with snapshot_session() as observer:
            rows = await observer.execute(select(User.username, User.proxy_settings))
            committed_at_reconcile.append(rows.all())
        return await real_reconcile(db)

    try:
        monkeypatch.setattr("app.operation.group.sync_users", AsyncMock())
        monkeypatch.setattr(core_module, "reconcile_wireguard_subnets", note_then_reconcile)
        responses = _race(monkeypatch, access_token, _give_the_group_wireguard(world), _resize(world, prefix, 28))

        assert [response.status_code for response in responses] == [status.HTTP_200_OK, status.HTTP_200_OK], [
            response.text for response in responses
        ]
        names = {holder["username"], world.member["username"]}
        final = {name: _user(access_token, name)["proxy_settings"]["wireguard"]["peer_ips"] for name in names}
        assert all(len(peer_ips) == 1 for peer_ips in final.values()), final
        assert len({peer_ips[0] for peer_ips in final.values()}) == 2, final
        assert all(peer_ips[0].startswith(f"{prefix}.") for peer_ips in final.values()), final
        for rows in committed_at_reconcile:
            held = [ip for peer_ips in _peer_ips_by_user(rows, names).values() for ip in peer_ips]
            assert len(held) == len(set(held)), f"an address was held twice before the reconcile: {held}"
        assert client.get(f"/api/core/{world.wg_core['id']}", headers=auth_headers(access_token)).json()["config"][
            "address"
        ] == [f"{prefix}.1/28"]
        assert _usage(access_token, f"{prefix}.0/28")["used"] == 2
    finally:
        monkeypatch.setattr(core_module, "reconcile_wireguard_subnets", real_reconcile)
        asyncio.run(snapshot_engine.dispose())
        delete_user(access_token, holder["username"])
        delete_group(access_token, wg_group["id"])
        _drop_member_world(access_token, world)


def test_a_core_resize_a_user_create_and_a_member_modify_together_end_unique_and_addressed(access_token, monkeypatch):
    prefix = "10.47.10"
    world = _member_world(access_token, prefix, 29)
    wg_group = create_group(access_token, name=unique_name("race_wg"), inbound_tags=[world.interface_name])
    holders = [
        create_user(access_token, group_ids=[wg_group["id"]], payload={"username": unique_name("race_h")})
        for _ in range(2)
    ]
    created = unique_name("race_new")
    try:

        async def create_a_member(racing_client, headers):
            return await racing_client.post(
                "/api/user",
                headers=headers,
                json={
                    "username": created,
                    "proxy_settings": {},
                    "group_ids": [wg_group["id"]],
                    "status": "active",
                    "data_limit": 1024 * 1024,
                    "data_limit_reset_strategy": "no_reset",
                },
            )

        async def modify_a_member(racing_client, headers):
            return await racing_client.put(
                f"/api/user/{holders[0]['username']}",
                headers=headers,
                json={"group_ids": [wg_group["id"]], "note": "edited during the resize"},
            )

        responses = _race_around_reconcile(
            monkeypatch, access_token, _resize(world, prefix, 28), [create_a_member, modify_a_member]
        )

        assert [response.status_code for response in responses] == [
            status.HTTP_200_OK,
            status.HTTP_201_CREATED,
            status.HTTP_200_OK,
        ], [response.text for response in responses]
        names = [holder["username"] for holder in holders] + [created]
        final = {name: _user(access_token, name)["proxy_settings"]["wireguard"]["peer_ips"] for name in names}
        assert all(len(peer_ips) == 1 for peer_ips in final.values()), final
        assert len({peer_ips[0] for peer_ips in final.values()}) == 3, final
        assert all(peer_ips[0].startswith(f"{prefix}.") for peer_ips in final.values()), final
        assert _usage(access_token, f"{prefix}.0/28")["used"] == 3
    finally:
        for name in [holder["username"] for holder in holders] + [created]:
            response = client.get(f"/api/user/{name}", headers=auth_headers(access_token))
            if response.status_code == status.HTTP_200_OK:
                delete_user(access_token, name)
        delete_group(access_token, wg_group["id"])
        _drop_member_world(access_token, world)


def test_a_user_create_that_loaded_its_groups_before_a_group_edit_still_gets_an_address(access_token, monkeypatch):
    if ":memory:" in DATABASE_URL:
        pytest.skip("needs a database that separate connections share")
    from app.operation.user import UserOperation

    prefix = "10.47.11"
    world = _member_world(access_token, prefix, 29)
    created = unique_name("race_new")
    racing_engine = create_async_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"timeout": 30} if IS_SQLITE else {}
    )
    racing_session = async_sessionmaker(racing_engine, autoflush=False, expire_on_commit=False)

    async def racing_db():
        async with racing_session() as db:
            yield db

    real_validate = UserOperation.validate_all_groups
    gate = {"loaded": None, "edited": None}

    async def validate_then_wait_for_the_edit(self, db, model, admin=None, **kwargs):
        groups = await real_validate(self, db, model, admin, **kwargs)
        if getattr(model, "username", None) == created:
            gate["loaded"].set()
            await asyncio.wait_for(gate["edited"].wait(), REACH_THE_LOCK_SECONDS)
        return groups

    monkeypatch.setattr(UserOperation, "validate_all_groups", validate_then_wait_for_the_edit)
    monkeypatch.setattr("app.operation.group.sync_users", AsyncMock())
    headers = auth_headers(access_token)

    async def run():
        gate["loaded"], gate["edited"] = asyncio.Event(), asyncio.Event()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as racing_client:
            creating = asyncio.create_task(
                racing_client.post(
                    "/api/user",
                    headers=headers,
                    json={
                        "username": created,
                        "proxy_settings": {},
                        "group_ids": [world.shared["id"]],
                        "status": "active",
                        "data_limit": 1024 * 1024,
                        "data_limit_reset_strategy": "no_reset",
                    },
                )
            )
            await asyncio.wait_for(gate["loaded"].wait(), REACH_THE_LOCK_SECONDS)
            edited = await _give_the_group_wireguard(world)(racing_client, headers)
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
        member_ips = _user(access_token, world.member["username"])["proxy_settings"]["wireguard"]["peer_ips"]
        new_ips = _user(access_token, created)["proxy_settings"]["wireguard"]["peer_ips"]
        assert member_ips == [f"{prefix}.2/32"]
        assert new_ips == [f"{prefix}.3/32"], (member_ips, new_ips)
    finally:
        _drop_member_world(access_token, world, usernames=[created])
