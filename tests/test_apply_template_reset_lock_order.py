from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.crud.wireguard import ensure_wireguard_subnet_pools
from app.db.models import CoreConfig, CoreType, Group, ProxyInbound, UserStatus, UserTemplate
from app.models.settings import General, HWIDSettings
from app.models.user import BulkUsersApplyTemplate
from app.operation import OperatorType, user as user_operation
from app.operation.user import UserOperation
from tests.test_record_usages import reset_user_usage_history_buffer, session_factory  # noqa: F401
from tests.test_reset_user_data_usage import _lifetime, _user, clear_dialect_cache  # noqa: F401


@pytest.mark.asyncio
async def test_apply_template_reset_takes_the_pool_locks_before_the_first_user_lock(session_factory, monkeypatch):  # noqa: F811
    async with session_factory() as db:
        inbound = ProxyInbound(tag="wg0")
        template_group = Group(name="order_template", inbounds=[inbound])
        template = UserTemplate(
            name="order_reset",
            username_prefix=None,
            username_suffix=None,
            extra_settings=None,
            groups=[template_group],
            data_limit=5_000,
            reset_usages=True,
        )
        core = CoreConfig(
            name="wg_order", config={"interface_name": "wg0", "address": ["10.78.0.1/24"]}, type=CoreType.wg
        )
        db_user = _user("order_applied", used_traffic=3_000, data_limit=1_000, status=UserStatus.limited)
        db.add_all([inbound, template_group, template, core, db_user])
        await db.flush()
        await ensure_wireguard_subnet_pools(db)
        await db.commit()
        user_id, template_id = db_user.id, template.id

    calls: list[str] = []
    ensure_pools = user_operation.ensure_wireguard_subnet_pools
    bulk_reset = user_operation.bulk_reset_user_data_usage

    async def record_pools(db):
        calls.append("pools")
        return await ensure_pools(db)

    async def record_reset(db, users, **kwargs):
        calls.append("reset")
        return await bulk_reset(db, users, **kwargs)

    async def no_op(*args, **kwargs):
        return None

    async def fake_url(user):
        return "https://sub.example/sub/token"

    monkeypatch.setattr(user_operation, "ensure_wireguard_subnet_pools", record_pools)
    monkeypatch.setattr(user_operation, "bulk_reset_user_data_usage", record_reset)
    monkeypatch.setattr(user_operation, "general_settings", AsyncMock(return_value=General()))
    monkeypatch.setattr(user_operation, "hwid_settings", AsyncMock(return_value=HWIDSettings()))
    monkeypatch.setattr(user_operation, "sync_users", no_op)
    monkeypatch.setattr(
        user_operation,
        "notification",
        SimpleNamespace(modify_user=no_op, reset_user_data_usage=no_op, user_status_change=no_op),
    )
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(fake_url))

    async with session_factory() as db:
        await UserOperation(OperatorType.API).bulk_apply_template_to_users(
            db,
            BulkUsersApplyTemplate(ids={user_id}, user_template_id=template_id),
            SimpleNamespace(is_owner=True, role=None, username="tester"),
        )

    assert calls[:2] == ["pools", "reset"]
    assert await _lifetime(session_factory, user_id) == (0, 3_000)
