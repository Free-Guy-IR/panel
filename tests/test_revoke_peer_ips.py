import asyncio
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import Select, Update, event, select, text, update

from app import notification
from app.db.crud.bulk import update_users_proxy_settings
from app.db.crud.user import get_user_by_id
from app.db.models import CoreConfig, Group, ProxyInbound, User
from app.fork.operation import user_extras as user_extras_module
from app.models.core import CoreType
from app.models.group import BulkGroup
from app.models.proxy import ShadowsocksMethods
from app.models.settings import HWIDSettings
from app.models.user import BulkUsersProxy, BulkUsersSelection, UserModify
from app.node import node_manager
from app.operation import OperatorType, user as user_operation_module
from app.operation.group import GroupOperation
from app.operation.user import UserOperation
from app.utils.crypto import generate_wireguard_keypair, get_wireguard_public_key
from tests.a1_bridge_harness import INFO, RecordingNode, isolate_node_manager, seed_group, seed_user, settle
from tests.api import TestSession, engine
from tests.api.helpers import unique_name

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)


def _quiet(monkeypatch) -> RecordingNode:
    node = RecordingNode(2201)
    isolate_node_manager(monkeypatch, node_manager, {2201: node})
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in (
        "user_status_change",
        "modify_user",
        "user_subscription_revoked",
        "reset_user_data_usage",
        "create_user",
    ):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(user_operation_module, "hwid_settings", AsyncMock(return_value=HWIDSettings()))
    return node


async def _stored_settings(user_id: int) -> dict:
    async with TestSession() as session:
        return (await session.execute(select(User.proxy_settings).where(User.id == user_id))).scalar_one()


async def test_a_revoke_that_loaded_the_user_before_a_group_change_does_not_write_back_a_released_peer_ip(
    monkeypatch,
):
    _quiet(monkeypatch)
    wg_tag = unique_name("adv_wg")
    async with TestSession() as session:
        session.add(
            CoreConfig(
                name=wg_tag,
                type=CoreType.wg,
                config={"interface_name": wg_tag, "address": ["10.127.0.1/24"], "listen_port": 51820},
            )
        )
        wg_group = Group(name=unique_name("adv_wg_group"), inbounds=[ProxyInbound(tag=wg_tag)])
        plain_group = Group(name=unique_name("adv_plain_group"), inbounds=[ProxyInbound(tag=unique_name("adv_p"))])
        session.add_all([wg_group, plain_group])
        await session.commit()
        wg_group_id, plain_group_id = wg_group.id, plain_group.id

    operation = UserOperation(OperatorType.API)
    first = await seed_user(plain_group_id)
    async with TestSession() as db:
        await operation.modify_user_by_id(db, first, UserModify(group_ids=[wg_group_id]), OWNER)
    first_ip = (await _stored_settings(first))["wireguard"]["peer_ips"]
    assert first_ip

    async with TestSession() as revoke_db:
        stale = await get_user_by_id(revoke_db, first)
        async with TestSession() as db:
            await operation.modify_user_by_id(db, first, UserModify(group_ids=[plain_group_id]), OWNER)
        assert (await _stored_settings(first))["wireguard"]["peer_ips"] == []
        await operation._revoke_user_sub(revoke_db, stale, OWNER)

    second = await seed_user(plain_group_id)
    async with TestSession() as db:
        await operation.modify_user_by_id(db, second, UserModify(group_ids=[wg_group_id]), OWNER)
    async with TestSession() as db:
        await operation.modify_user_by_id(db, first, UserModify(group_ids=[wg_group_id]), OWNER)
    await settle()

    first_now = (await _stored_settings(first))["wireguard"]["peer_ips"]
    second_now = (await _stored_settings(second))["wireguard"]["peer_ips"]
    assert not set(first_now) & set(second_now), (
        f"user {first} lost WireGuard access (its peer IP {first_ip} went back to the pool) while a revoke that had "
        f"loaded it earlier was running; the revoke wrote {first_ip} back, user {second} was then given the same "
        f"address, and when {first} regained access both hold {sorted(set(first_now) & set(second_now))}"
    )


async def test_a_bulk_revoke_that_loaded_the_users_before_a_group_change_does_not_write_back_a_released_peer_ip(
    monkeypatch,
):
    _quiet(monkeypatch)
    wg_tag = unique_name("adv_wg")
    async with TestSession() as session:
        session.add(
            CoreConfig(
                name=wg_tag,
                type=CoreType.wg,
                config={"interface_name": wg_tag, "address": ["10.128.0.1/24"], "listen_port": 51820},
            )
        )
        wg_group = Group(name=unique_name("adv_wg_group"), inbounds=[ProxyInbound(tag=wg_tag)])
        plain_group = Group(name=unique_name("adv_plain_group"), inbounds=[ProxyInbound(tag=unique_name("adv_p"))])
        session.add_all([wg_group, plain_group])
        await session.commit()
        wg_group_id, plain_group_id = wg_group.id, plain_group.id

    operation = UserOperation(OperatorType.API)
    first = await seed_user(plain_group_id)
    async with TestSession() as db:
        await operation.modify_user_by_id(db, first, UserModify(group_ids=[wg_group_id]), OWNER)
    first_ip = (await _stored_settings(first))["wireguard"]["peer_ips"]
    assert first_ip

    stale_users = []
    real_get = operation._get_validated_users_by_ids

    async def load_then_change_the_group(db, ids, admin, **kwargs):
        users = await real_get(db, ids, admin, **kwargs)
        stale_users.extend(users)
        async with TestSession() as other:
            await operation.modify_user_by_id(other, first, UserModify(group_ids=[plain_group_id]), OWNER)
        return users

    monkeypatch.setattr(operation, "_get_validated_users_by_ids", load_then_change_the_group)
    async with TestSession() as revoke_db:
        await operation.bulk_revoke_user_sub(revoke_db, BulkUsersSelection(ids={first}), OWNER)
    monkeypatch.setattr(operation, "_get_validated_users_by_ids", real_get)

    assert stale_users
    after_revoke = (await _stored_settings(first))["wireguard"]["peer_ips"]
    assert after_revoke == [], after_revoke


async def _two_wireguard_groups(first_address: str, second_address: str) -> tuple[int, int, int]:
    first_tag = unique_name("adv_wg_a")
    second_tag = unique_name("adv_wg_b")
    async with TestSession() as session:
        session.add_all(
            [
                CoreConfig(
                    name=first_tag,
                    type=CoreType.wg,
                    config={"interface_name": first_tag, "address": [first_address], "listen_port": 51820},
                ),
                CoreConfig(
                    name=second_tag,
                    type=CoreType.wg,
                    config={"interface_name": second_tag, "address": [second_address], "listen_port": 51821},
                ),
            ]
        )
        first_group = Group(name=unique_name("adv_wg_a_group"), inbounds=[ProxyInbound(tag=first_tag)])
        second_group = Group(name=unique_name("adv_wg_b_group"), inbounds=[ProxyInbound(tag=second_tag)])
        plain_group = Group(name=unique_name("adv_plain_group"), inbounds=[ProxyInbound(tag=unique_name("adv_p"))])
        session.add_all([first_group, second_group, plain_group])
        await session.commit()
        return first_group.id, second_group.id, plain_group.id


async def test_a_revoke_that_loaded_the_user_before_its_wireguard_address_moved_writes_the_new_address(
    monkeypatch,
):
    _quiet(monkeypatch)
    first_group_id, second_group_id, plain_group_id = await _two_wireguard_groups("10.126.0.1/24", "10.125.0.1/24")

    operation = UserOperation(OperatorType.API)
    moved = await seed_user(plain_group_id)
    async with TestSession() as db:
        await operation.modify_user_by_id(db, moved, UserModify(group_ids=[first_group_id]), OWNER)
    old_ips = (await _stored_settings(moved))["wireguard"]["peer_ips"]
    assert old_ips and old_ips[0].startswith("10.126.")

    async with TestSession() as revoke_db:
        stale = await get_user_by_id(revoke_db, moved)
        async with TestSession() as db:
            await operation.modify_user_by_id(db, moved, UserModify(group_ids=[second_group_id]), OWNER)
        new_ips = (await _stored_settings(moved))["wireguard"]["peer_ips"]
        assert new_ips and new_ips[0].startswith("10.125.")
        await operation._revoke_user_sub(revoke_db, stale, OWNER)

    after_revoke = (await _stored_settings(moved))["wireguard"]["peer_ips"]
    assert after_revoke == new_ips, (
        f"user {moved} moved from {old_ips} to {new_ips} while a revoke that had loaded it earlier was running; the "
        f"revoke stored {after_revoke}"
    )


async def test_a_bulk_revoke_that_loaded_the_users_before_their_wireguard_addresses_moved_writes_the_new_addresses(
    monkeypatch,
):
    _quiet(monkeypatch)
    first_group_id, second_group_id, plain_group_id = await _two_wireguard_groups("10.124.0.1/24", "10.123.0.1/24")

    operation = UserOperation(OperatorType.API)
    moved = [await seed_user(plain_group_id) for _ in range(2)]
    for user_id in moved:
        async with TestSession() as db:
            await operation.modify_user_by_id(db, user_id, UserModify(group_ids=[first_group_id]), OWNER)
    old_ips = {user_id: (await _stored_settings(user_id))["wireguard"]["peer_ips"] for user_id in moved}
    assert all(ips and ips[0].startswith("10.124.") for ips in old_ips.values()), old_ips

    new_ips = {}
    real_get = operation._get_validated_users_by_ids

    async def load_then_move_the_users(db, ids, admin, **kwargs):
        users = await real_get(db, ids, admin, **kwargs)
        for user_id in moved:
            async with TestSession() as other:
                await operation.modify_user_by_id(other, user_id, UserModify(group_ids=[second_group_id]), OWNER)
            new_ips[user_id] = (await _stored_settings(user_id))["wireguard"]["peer_ips"]
        return users

    monkeypatch.setattr(operation, "_get_validated_users_by_ids", load_then_move_the_users)
    async with TestSession() as revoke_db:
        await operation.bulk_revoke_user_sub(revoke_db, BulkUsersSelection(ids=set(moved)), OWNER)
    monkeypatch.setattr(operation, "_get_validated_users_by_ids", real_get)

    assert all(ips and ips[0].startswith("10.123.") for ips in new_ips.values()), new_ips
    after_revoke = {user_id: (await _stored_settings(user_id))["wireguard"]["peer_ips"] for user_id in moved}
    assert after_revoke == new_ips, (
        f"users {moved} moved from {old_ips} to {new_ips} while a bulk revoke that had loaded them earlier was "
        f"running; the revoke stored {after_revoke}"
    )


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_a_revoke_that_loaded_the_user_before_its_method_and_flow_changed_keeps_the_new_ones(monkeypatch, bulk):
    _quiet(monkeypatch)
    plain_group_id = (await _two_wireguard_groups("10.122.0.1/24", "10.121.0.1/24"))[2]
    operation = UserOperation(OperatorType.API)
    user_id = await seed_user(plain_group_id)
    before = await _stored_settings(user_id)
    assert before["shadowsocks"]["method"] != ShadowsocksMethods.AES_256_GCM.value
    assert not before["vless"].get("flow")

    async def change_the_flow_and_the_method():
        async with TestSession() as other:
            await operation.modify_user_by_id(
                other, user_id, UserModify(proxy_settings={"vless": {"flow": "xtls-rprx-vision"}}), OWNER
            )
        async with TestSession() as other:
            await update_users_proxy_settings(
                other, BulkUsersProxy(users={user_id}, method=ShadowsocksMethods.AES_256_GCM)
            )
        changed = await _stored_settings(user_id)
        assert changed["vless"]["flow"] == "xtls-rprx-vision"
        assert changed["shadowsocks"]["method"] == ShadowsocksMethods.AES_256_GCM.value

    if bulk:
        real_get = operation._get_validated_users_by_ids

        async def load_then_change(db, ids, admin, **kwargs):
            users = await real_get(db, ids, admin, **kwargs)
            await change_the_flow_and_the_method()
            return users

        monkeypatch.setattr(operation, "_get_validated_users_by_ids", load_then_change)
        async with TestSession() as revoke_db:
            await operation.bulk_revoke_user_sub(revoke_db, BulkUsersSelection(ids={user_id}), OWNER)
        monkeypatch.setattr(operation, "_get_validated_users_by_ids", real_get)
    else:
        async with TestSession() as revoke_db:
            stale = await get_user_by_id(revoke_db, user_id)
            await change_the_flow_and_the_method()
            await operation._revoke_user_sub(revoke_db, stale, OWNER)

    after = await _stored_settings(user_id)
    assert after["vless"]["id"] != before["vless"]["id"]
    assert (after["shadowsocks"]["method"], after["vless"]["flow"]) == (
        ShadowsocksMethods.AES_256_GCM.value,
        "xtls-rprx-vision",
    ), (
        f"user {user_id}'s method and flow changed while a revoke that had loaded it earlier was running; the revoke "
        f"stored method {after['shadowsocks']['method']!r} and flow {after['vless']['flow']!r}"
    )


async def test_a_revoke_that_loaded_the_user_before_it_lost_wireguard_clears_its_keys_and_writes_no_old_address(
    monkeypatch,
):
    _quiet(monkeypatch)
    first_group_id, _, plain_group_id = await _two_wireguard_groups("10.120.0.1/24", "10.119.0.1/24")
    operation = UserOperation(OperatorType.API)
    user_id = await seed_user(plain_group_id)
    async with TestSession() as db:
        await operation.modify_user_by_id(db, user_id, UserModify(group_ids=[first_group_id]), OWNER)
    old = (await _stored_settings(user_id))["wireguard"]
    assert old["public_key"] and old["peer_ips"]
    private_key, public_key = generate_wireguard_keypair()

    async with TestSession() as revoke_db:
        stale = await get_user_by_id(revoke_db, user_id)
        async with TestSession() as db:
            await operation.modify_user_by_id(
                db,
                user_id,
                UserModify(proxy_settings={"wireguard": {"private_key": private_key, "public_key": public_key}}),
                OWNER,
            )
        async with TestSession() as db:
            await operation.modify_user_by_id(db, user_id, UserModify(group_ids=[plain_group_id]), OWNER)
        changed = (await _stored_settings(user_id))["wireguard"]
        assert (changed["private_key"], changed["public_key"], changed["peer_ips"]) == (private_key, public_key, [])
        await operation._revoke_user_sub(revoke_db, stale, OWNER)

    after = (await _stored_settings(user_id))["wireguard"]
    assert (after["private_key"], after["public_key"], after["peer_ips"]) == (None, None, []), (
        f"user {user_id} changed its WireGuard keys and then lost WireGuard access while a revoke that had loaded it "
        f"earlier was running; the revoke stored public key {after['public_key']} (old {old['public_key']}, changed "
        f"to {public_key}) and peer IPs {after['peer_ips']} instead of no keys and no address"
    )


LOCK_WAITERS = {
    "mysql": "SELECT COUNT(*) FROM information_schema.INNODB_TRX WHERE trx_state = 'LOCK WAIT'",
    "postgresql": (
        "SELECT COUNT(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND datname = current_database()"
    ),
}
BOUNDED_WAITS = {"mysql": "SET SESSION innodb_lock_wait_timeout = 10", "postgresql": "SET lock_timeout = '10s'"}


async def _until_a_transaction_waits_for_a_row_lock(watcher, dialect: str) -> None:
    for _ in range(200):
        if await watcher.scalar(text(LOCK_WAITERS[dialect])):
            return
        await watcher.rollback()
        await asyncio.sleep(0.1)
    raise AssertionError("the bulk revoke never waited for the user row the usage recorder holds")


def _recorder_lock(user_id: int):
    return select(User.id, User.usage_epoch).where(User.id.in_([user_id])).order_by(User.id).with_for_update()


async def test_a_bulk_revoke_takes_the_user_rows_in_the_usage_recorders_order(monkeypatch):
    dialect = "mysql" if engine.dialect.name in ("mysql", "mariadb") else engine.dialect.name
    if dialect not in LOCK_WAITERS:
        pytest.skip(reason="row locks and a deadlock detector are what this proves; SQLite locks the whole file")
    _quiet(monkeypatch)
    wireguard_group_id, _, plain_group_id = await _two_wireguard_groups("10.118.0.1/24", "10.117.0.1/24")
    first, second = sorted([await seed_user(plain_group_id), await seed_user(plain_group_id)])
    operation = UserOperation(OperatorType.API)
    async with TestSession() as db:
        await operation.modify_user_by_id(db, first, UserModify(group_ids=[wireguard_group_id]), OWNER)
    assert (await _stored_settings(first))["wireguard"]["peer_ips"]
    assert not (await _stored_settings(second))["wireguard"]["peer_ips"]

    async def revoke():
        async with TestSession() as db:
            await operation.bulk_revoke_user_sub(db, BulkUsersSelection(ids={first, second}), OWNER)

    recorder_engine = engine.execution_options(isolation_level="READ COMMITTED")
    async with recorder_engine.connect() as recorder, engine.connect() as watcher:
        await recorder.execute(text(BOUNDED_WAITS[dialect]))
        await recorder.execute(_recorder_lock(first))
        revoking = asyncio.create_task(revoke())
        try:
            await _until_a_transaction_waits_for_a_row_lock(watcher, dialect)
            await recorder.execute(_recorder_lock(second))
            await recorder.execute(
                update(User).where(User.id.in_([first, second])).values(used_traffic=User.used_traffic + 1)
            )
            await recorder.commit()
        except BaseException:
            await recorder.rollback()
            raise
        finally:
            outcome = (await asyncio.gather(asyncio.wait_for(revoking, timeout=60), return_exceptions=True))[0]
    assert outcome is None, f"the bulk revoke failed after the usage recorder let go of its rows: {outcome!r}"

    async with TestSession() as session:
        rows = (
            await session.execute(
                select(User.id, User.used_traffic, User.sub_revoked_at).where(User.id.in_([first, second]))
            )
        ).all()
    assert sorted((user_id, used, revoked is not None) for user_id, used, revoked in rows) == [
        (first, 1, True),
        (second, 1, True),
    ]


def _lock_order_recorder(order: list[str]):
    def before_execute(conn, statement, multiparams, params, execution_options):
        if isinstance(statement, Update) and statement.table.name in ("named_locks", "users"):
            order.append("named lock" if statement.table.name == "named_locks" else "user row")
        elif (
            isinstance(statement, Select)
            and statement._for_update_arg is not None
            and any(getattr(table, "name", None) == "users" for table in statement.get_final_froms())
        ):
            order.append("user row")

    return before_execute


@pytest.mark.parametrize("revoked", ["one-without-wireguard", "one-with-wireguard", "both-in-bulk"])
async def test_a_revoke_takes_the_proxy_secrets_lock_before_any_user_row(monkeypatch, revoked):
    _quiet(monkeypatch)
    wireguard_group_id, _, plain_group_id = await _two_wireguard_groups("10.116.0.1/24", "10.115.0.1/24")
    operation = UserOperation(OperatorType.API)
    without_wireguard, with_wireguard = await seed_user(plain_group_id), await seed_user(plain_group_id)
    async with TestSession() as db:
        await operation.modify_user_by_id(db, with_wireguard, UserModify(group_ids=[wireguard_group_id]), OWNER)

    order: list[str] = []
    recorder = _lock_order_recorder(order)
    event.listen(engine.sync_engine, "before_execute", recorder)
    try:
        async with TestSession() as db:
            if revoked == "both-in-bulk":
                await operation.bulk_revoke_user_sub(
                    db, BulkUsersSelection(ids={with_wireguard, without_wireguard}), OWNER
                )
            else:
                user_id = without_wireguard if revoked == "one-without-wireguard" else with_wireguard
                await operation._revoke_user_sub(db, await get_user_by_id(db, user_id), OWNER)
    finally:
        event.remove(engine.sync_engine, "before_execute", recorder)

    assert "named lock" in order and "user row" in order, order
    assert order.index("named lock") < order.index("user row"), (
        f"a revoke ({revoked}) locked a user row before the proxy secrets lock, the reverse of every other secrets "
        f"writer, so it can deadlock against them: {order}"
    )


async def test_a_bulk_revoke_never_gives_two_of_its_users_the_same_new_secret(monkeypatch):
    _quiet(monkeypatch)
    plain_group_id = (await _two_wireguard_groups("10.114.0.1/24", "10.113.0.1/24"))[2]
    operation = UserOperation(OperatorType.API)
    first, second = await seed_user(plain_group_id), await seed_user(plain_group_id)
    old_ids = {user_id: (await _stored_settings(user_id))["vless"]["id"] for user_id in (first, second)}
    colliding = str(uuid4())
    real_build = user_extras_module.build_revoked_proxy_settings

    def build_with_a_colliding_vless_id(db_user):
        settings = real_build(db_user)
        settings["vless"]["id"] = colliding
        return settings

    monkeypatch.setattr(user_extras_module, "build_revoked_proxy_settings", build_with_a_colliding_vless_id)
    async with TestSession() as db:
        await operation.bulk_revoke_user_sub(db, BulkUsersSelection(ids={first, second}), OWNER)

    new_ids = {user_id: (await _stored_settings(user_id))["vless"]["id"] for user_id in (first, second)}
    assert new_ids[first] != new_ids[second], (
        f"a bulk revoke generated the same VLESS id for users {first} and {second} and stored it for both: {new_ids}"
    )
    assert not set(new_ids.values()) & set(old_ids.values()), (old_ids, new_ids)


async def _plant_wireguard_keys(user_ids, private_key: str, public_key: str) -> None:
    async with TestSession() as session:
        for user_id in user_ids:
            stored = (await session.execute(select(User.proxy_settings).where(User.id == user_id))).scalar_one()
            wireguard = {**(stored.get("wireguard") or {}), "private_key": private_key, "public_key": public_key}
            await session.execute(
                update(User).where(User.id == user_id).values(proxy_settings={**stored, "wireguard": wireguard})
            )
        await session.commit()


async def _holders_of(public_key: str) -> list[int]:
    column = User.proxy_settings["wireguard"]["public_key"].as_string()
    async with TestSession() as session:
        return sorted((await session.execute(select(User.id).where(column == public_key))).scalars().all())


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_a_revoke_clears_the_wireguard_keys_of_a_user_without_wireguard_access(monkeypatch, bulk):
    _quiet(monkeypatch)
    wireguard_group_id, _, plain_group_id = await _two_wireguard_groups("10.129.0.1/24", "10.130.0.1/24")
    operation = UserOperation(OperatorType.API)
    revoked, bystander, entitled = [await seed_user(plain_group_id) for _ in range(3)]
    async with TestSession() as db:
        await operation.modify_user_by_id(db, entitled, UserModify(group_ids=[wireguard_group_id]), OWNER)
    entitled_before = (await _stored_settings(entitled))["wireguard"]
    shared_private, shared_public = generate_wireguard_keypair()
    await _plant_wireguard_keys([revoked, bystander], shared_private, shared_public)

    if bulk:
        async with TestSession() as db:
            await operation.bulk_revoke_user_sub(db, BulkUsersSelection(ids={revoked, entitled}), OWNER)
    else:
        for user_id in (revoked, entitled):
            async with TestSession() as db:
                await operation._revoke_user_sub(db, await get_user_by_id(db, user_id), OWNER)

    after = (await _stored_settings(revoked))["wireguard"]
    assert (after.get("private_key"), after.get("public_key"), after.get("peer_ips")) == (None, None, []), (
        f"user {revoked} has no WireGuard access and was revoked, but it still holds the WireGuard keypair it "
        f"shared with user {bystander}: {after}"
    )
    assert await _holders_of(shared_public) == [bystander]
    entitled_after = (await _stored_settings(entitled))["wireguard"]
    assert entitled_after["public_key"] not in (None, entitled_before["public_key"])
    assert get_wireguard_public_key(entitled_after["private_key"]) == entitled_after["public_key"]
    assert entitled_after["peer_ips"] == entitled_before["peer_ips"]
    assert await _holders_of(entitled_after["public_key"]) == [entitled]


MOVES = ("revoke", "edit", "group change")
GRANTS = {"grants-wireguard": True, "keeps-no-wireguard": False}


async def _interleaving_setup(monkeypatch, first_address: str, second_address: str, *, grants_wireguard: bool):
    node = _quiet(monkeypatch)
    await node.connect(INFO.node_version, INFO.core_version)
    pushed: list[tuple[str, str | None, list[str]]] = []
    deliver = node._apply

    def record_and_deliver(kind, users):
        for user in users:
            wireguard = user.proxies.wireguard
            pushed.append((user.email, wireguard.public_key or None, list(wireguard.peer_ips)))
        deliver(kind, users)

    monkeypatch.setattr(node, "_apply", record_and_deliver)
    wireguard_group_id, _, plain_group_id = await _two_wireguard_groups(first_address, second_address)
    added_group_id = wireguard_group_id if grants_wireguard else await seed_group(unique_name("adv_p_added"))
    target, bystander = await seed_user(plain_group_id), await seed_user(plain_group_id)
    shared_private, shared_public = generate_wireguard_keypair()
    await _plant_wireguard_keys([target, bystander], shared_private, shared_public)
    groups = GroupOperation(operator_type=OperatorType.API)
    groups.check_inbound_tags = AsyncMock(return_value=None)
    return SimpleNamespace(
        grants_wireguard=grants_wireguard,
        added_group_id=added_group_id,
        target=target,
        bystander=bystander,
        shared_private=shared_private,
        shared_public=shared_public,
        before=await _stored_settings(target),
        users=UserOperation(OperatorType.API),
        groups=groups,
        pushed=pushed,
    )


def _edit_request(case) -> UserModify:
    proxy_settings = {"vless": {"flow": "xtls-rprx-vision"}}
    if not case.grants_wireguard:
        proxy_settings["wireguard"] = {"private_key": case.shared_private, "public_key": case.shared_public}
    return UserModify(proxy_settings=proxy_settings)


async def _run_move(case, move: str, revoke_db=None, stale=None) -> None:
    if move == "revoke":
        await case.users._revoke_user_sub(revoke_db, stale, OWNER)
        return
    async with TestSession() as db:
        if move == "edit":
            await case.users.modify_user_by_id(db, case.target, _edit_request(case), OWNER)
        else:
            await case.groups.bulk_add_groups(
                db, BulkGroup(group_ids={case.added_group_id}, users={case.target}), OWNER
            )


async def _assert_the_wireguard_keys_ended_right(case, wireguard: dict, label: str) -> None:
    if not case.grants_wireguard:
        assert (wireguard.get("private_key"), wireguard.get("public_key"), wireguard.get("peer_ips")) == (
            None,
            None,
            [],
        ), (
            f"{label}: user {case.target} never had WireGuard access, was revoked and was sent the WireGuard pair it "
            f"shared with user {case.bystander}, but it ends with {wireguard}"
        )
        return
    assert wireguard.get("public_key") and wireguard["public_key"] != case.shared_public, (
        f"{label}: user {case.target} got WireGuard access and was revoked, but it ends with WireGuard keys "
        f"{wireguard.get('public_key')!r} (the shared pair is {case.shared_public!r})"
    )
    assert get_wireguard_public_key(wireguard["private_key"]) == wireguard["public_key"], label
    assert await _holders_of(wireguard["public_key"]) == [case.target], label
    assert len(wireguard["peer_ips"]) == 1, (label, wireguard["peer_ips"])


async def _assert_the_interleaving_ended_consistent(case, label: str) -> None:
    await settle()
    after = await _stored_settings(case.target)
    wireguard = after["wireguard"]
    await _assert_the_wireguard_keys_ended_right(case, wireguard, label)
    assert await _holders_of(case.shared_public) == [case.bystander], label
    assert after["vless"]["flow"] == "xtls-rprx-vision", (label, after["vless"])
    assert after["vless"]["id"] != case.before["vless"]["id"], label
    async with TestSession() as session:
        revoked_at = (await session.execute(select(User.sub_revoked_at).where(User.id == case.target))).scalar_one()
    assert revoked_at is not None, label
    pushes = [push for push in case.pushed if push[0] == str(case.target)]
    assert pushes, f"{label}: nothing about user {case.target} reached the node"
    assert pushes[-1] == (str(case.target), wireguard.get("public_key"), wireguard.get("peer_ips") or []), (
        f"{label}: the node was last sent WireGuard key {pushes[-1][1]!r} and peer IPs {pushes[-1][2]} for user "
        f"{case.target}, but the database holds {wireguard.get('public_key')!r} and {wireguard.get('peer_ips')}; "
        f"every push for the user: {pushes}"
    )


@pytest.mark.parametrize("grants", GRANTS)
@pytest.mark.parametrize("order", list(itertools.permutations(MOVES)), ids=" then ".join)
async def test_a_revoke_interleaved_with_an_edit_and_a_group_change_ends_consistent(monkeypatch, order, grants):
    case = await _interleaving_setup(monkeypatch, "10.131.0.1/24", "10.132.0.1/24", grants_wireguard=GRANTS[grants])
    async with TestSession() as revoke_db:
        stale = await get_user_by_id(revoke_db, case.target)
        for move in order:
            await _run_move(case, move, revoke_db, stale)
    await _assert_the_interleaving_ended_consistent(case, f"{grants}: {' then '.join(order)}")


@pytest.mark.parametrize("grants", GRANTS)
async def test_a_revoke_racing_an_edit_and_a_group_change_ends_consistent(monkeypatch, grants):
    if engine.dialect.name not in ("mysql", "mariadb", "postgresql"):
        pytest.skip(reason="real concurrency needs row locks; SQLite serializes whole-file writers")
    case = await _interleaving_setup(monkeypatch, "10.133.0.1/24", "10.134.0.1/24", grants_wireguard=GRANTS[grants])

    async def revoke():
        async with TestSession() as revoke_db:
            await _run_move(case, "revoke", revoke_db, await get_user_by_id(revoke_db, case.target))

    outcomes = await asyncio.gather(
        asyncio.wait_for(revoke(), timeout=60),
        asyncio.wait_for(_run_move(case, "edit"), timeout=60),
        asyncio.wait_for(_run_move(case, "group change"), timeout=60),
        return_exceptions=True,
    )
    assert outcomes == [None, None, None], outcomes
    await _assert_the_interleaving_ended_consistent(case, f"{grants}: concurrent")
