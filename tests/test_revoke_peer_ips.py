from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app import notification
from app.db.crud.user import get_user_by_id
from app.db.models import CoreConfig, Group, ProxyInbound, User
from app.models.core import CoreType
from app.models.settings import HWIDSettings
from app.models.user import BulkUsersSelection, UserModify
from app.node import node_manager
from app.operation import OperatorType, user as user_operation_module
from app.operation.user import UserOperation
from tests.a1_bridge_harness import RecordingNode, isolate_node_manager, seed_user, settle
from tests.api import TestSession
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
