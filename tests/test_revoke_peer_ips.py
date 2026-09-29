import asyncio
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
from app.models.proxy import ShadowsocksMethods
from app.models.settings import HWIDSettings
from app.models.user import BulkUsersProxy, BulkUsersSelection, UserModify
from app.node import node_manager
from app.operation import OperatorType, user as user_operation_module
from app.operation.user import UserOperation
from app.utils.crypto import generate_wireguard_keypair
from tests.a1_bridge_harness import RecordingNode, isolate_node_manager, seed_user, settle
from tests.api import TestSession, engine
from tests.api.helpers import unique_name

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)


def _quiet(monkeypatch):
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
                config={"interface_name": wg_tag, "address": ["10.97.0.1/24"], "listen_port": 51820},
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
                config={"interface_name": wg_tag, "address": ["10.98.0.1/24"], "listen_port": 51820},
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
    first_group_id, second_group_id, plain_group_id = await _two_wireguard_groups("10.96.0.1/24", "10.95.0.1/24")

    operation = UserOperation(OperatorType.API)
    moved = await seed_user(plain_group_id)
    async with TestSession() as db:
        await operation.modify_user_by_id(db, moved, UserModify(group_ids=[first_group_id]), OWNER)
    old_ips = (await _stored_settings(moved))["wireguard"]["peer_ips"]
    assert old_ips and old_ips[0].startswith("10.96.")

    async with TestSession() as revoke_db:
        stale = await get_user_by_id(revoke_db, moved)
        async with TestSession() as db:
            await operation.modify_user_by_id(db, moved, UserModify(group_ids=[second_group_id]), OWNER)
        new_ips = (await _stored_settings(moved))["wireguard"]["peer_ips"]
        assert new_ips and new_ips[0].startswith("10.95.")
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
    first_group_id, second_group_id, plain_group_id = await _two_wireguard_groups("10.94.0.1/24", "10.93.0.1/24")

    operation = UserOperation(OperatorType.API)
    moved = [await seed_user(plain_group_id) for _ in range(2)]
    for user_id in moved:
        async with TestSession() as db:
            await operation.modify_user_by_id(db, user_id, UserModify(group_ids=[first_group_id]), OWNER)
    old_ips = {user_id: (await _stored_settings(user_id))["wireguard"]["peer_ips"] for user_id in moved}
    assert all(ips and ips[0].startswith("10.94.") for ips in old_ips.values()), old_ips

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

    assert all(ips and ips[0].startswith("10.93.") for ips in new_ips.values()), new_ips
    after_revoke = {user_id: (await _stored_settings(user_id))["wireguard"]["peer_ips"] for user_id in moved}
    assert after_revoke == new_ips, (
        f"users {moved} moved from {old_ips} to {new_ips} while a bulk revoke that had loaded them earlier was "
        f"running; the revoke stored {after_revoke}"
    )


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_a_revoke_that_loaded_the_user_before_its_method_and_flow_changed_keeps_the_new_ones(monkeypatch, bulk):
    _quiet(monkeypatch)
    plain_group_id = (await _two_wireguard_groups("10.92.0.1/24", "10.91.0.1/24"))[2]
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


async def test_a_revoke_that_loaded_the_user_before_its_keys_changed_and_it_lost_wireguard_keeps_the_new_keys(
    monkeypatch,
):
    _quiet(monkeypatch)
    first_group_id, _, plain_group_id = await _two_wireguard_groups("10.90.0.1/24", "10.89.0.1/24")
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
    assert (after["private_key"], after["public_key"], after["peer_ips"]) == (private_key, public_key, []), (
        f"user {user_id} changed its WireGuard keys and then lost WireGuard access while a revoke that had loaded it "
        f"earlier was running; the revoke stored public key {after['public_key']} (old {old['public_key']}, new "
        f"{public_key}) and peer IPs {after['peer_ips']}"
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
    wireguard_group_id, _, plain_group_id = await _two_wireguard_groups("10.88.0.1/24", "10.87.0.1/24")
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
    wireguard_group_id, _, plain_group_id = await _two_wireguard_groups("10.86.0.1/24", "10.85.0.1/24")
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
    plain_group_id = (await _two_wireguard_groups("10.84.0.1/24", "10.83.0.1/24"))[2]
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
