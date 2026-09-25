import asyncio

import pytest
from alembic.command import downgrade, upgrade
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import create_engine, text

import app.operation.subscription as subscription_module
from app.models.settings import HWIDSettings
from app.models.user import MAX_HWID_LIMIT, UserModify
from app.models.user_template import UserTemplateModify
from app.node.user import node_connection_cap
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation

SANITIZE_REVISION = "6698b9a695e3"
SANITIZE_PARENT = "b176d23e434c"
USER_LIMITS = {1: -1, 2: -7, 3: 0, 4: 3, 5: None, 6: 2**31 + 5}
TEMPLATE_LIMITS = {1: -3, 2: 0, 3: 4, 4: None}
POLICIES = (
    HWIDSettings(enabled=True, forced=False, fallback_limit=2),
    HWIDSettings(enabled=True, forced=True, fallback_limit=2),
    HWIDSettings(enabled=True, forced=False, fallback_limit=None),
    HWIDSettings(enabled=True, forced=True, fallback_limit=None),
    HWIDSettings(enabled=False, forced=True, fallback_limit=2),
)
REGISTERED_DEVICES = (0, 1, 2, 3, 5, 10_000)


def _run(url, action, revision):
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url)
    with create_engine(url).begin() as connection:
        config.attributes["connection"] = connection
        action(config, revision)


def _seed(url):
    with create_engine(url).begin() as connection:
        for user_id, limit in USER_LIMITS.items():
            connection.execute(
                text(
                    "INSERT INTO users (id, username, status, used_traffic, data_limit_reset_strategy, proxy_settings, "
                    "created_at, hwid_limit) VALUES (:id, :name, 'active', 0, 'no_reset', '{}', '2026-08-08 00:00:00', "
                    ":limit)"
                ),
                {"id": user_id, "name": f"u{user_id}", "limit": limit},
            )
        for template_id, limit in TEMPLATE_LIMITS.items():
            connection.execute(
                text(
                    "INSERT INTO user_templates (id, name, data_limit, expire_duration, username_prefix, "
                    "username_suffix, hwid_limit, data_limit_reset_strategy) "
                    "VALUES (:id, :name, 0, 0, '', '', :limit, 'no_reset')"
                ),
                {"id": template_id, "name": f"t{template_id}", "limit": limit},
            )


def _stored(url):
    with create_engine(url).connect() as connection:
        return {
            "users": dict(connection.execute(text("SELECT id, hwid_limit FROM users")).all()),
            "user_templates": dict(connection.execute(text("SELECT id, hwid_limit FROM user_templates")).all()),
        }


def _new_device_outcome(monkeypatch, hwid_limit, policy, registered_devices, header):
    registrations = []

    async def settings():
        return policy

    async def covers(_db, _user_id, _conf):
        return True

    async def known_device(_db, _user_id, _value):
        return None

    async def device_count(_db, _user_id):
        return registered_devices

    async def register(_db, _user_id, value, *_details):
        registrations.append(value)

    monkeypatch.setattr(subscription_module, "hwid_settings", settings)
    monkeypatch.setattr(subscription_module, "hwid_covers_user", covers)
    monkeypatch.setattr(subscription_module, "get_user_hwid_by_value", known_device)
    monkeypatch.setattr(subscription_module, "get_user_hwid_count", device_count)
    monkeypatch.setattr(subscription_module, "register_user_hwid", register)
    operation = SubscriptionOperation(OperatorType.API)
    try:
        asyncio.run(operation.validate_and_register_hwid(None, 1, hwid_limit, None, header, None, None, None))
    except HTTPException as exc:
        return f"{exc.status_code} {exc.detail}"
    return "registered" if registrations else "served without registering"


def _device_policy(monkeypatch, hwid_limit):
    return [
        _new_device_outcome(monkeypatch, hwid_limit, policy, registered, header)
        for policy in POLICIES
        for registered in REGISTERED_DEVICES
        for header in ("a-new-device", None)
    ]


def _device_policies(monkeypatch, stored):
    return {
        (table, row_id): _device_policy(monkeypatch, limit)
        for table, rows in stored.items()
        for row_id, limit in rows.items()
    }


def test_the_sanitize_migration_keeps_every_users_and_templates_device_cap(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'hwid.sqlite3'}"
    _run(url, upgrade, SANITIZE_PARENT)
    _seed(url)
    before = _device_policies(monkeypatch, _stored(url))
    assert before[("users", 1)] != before[("users", 5)]
    assert before[("user_templates", 1)] != before[("user_templates", 4)]

    _run(url, upgrade, SANITIZE_REVISION)
    after = _stored(url)

    assert after["users"] == {1: MAX_HWID_LIMIT, 2: MAX_HWID_LIMIT, 3: 0, 4: 3, 5: None, 6: MAX_HWID_LIMIT}
    assert after["user_templates"] == {1: MAX_HWID_LIMIT, 2: 0, 3: 4, 4: None}
    assert _device_policies(monkeypatch, after) == before


def test_every_sanitized_limit_survives_a_full_round_trip_and_reaches_a_node(tmp_path):
    url = f"sqlite:///{tmp_path / 'hwid_round_trip.sqlite3'}"
    _run(url, upgrade, SANITIZE_PARENT)
    _seed(url)
    _run(url, upgrade, SANITIZE_REVISION)
    stored = _stored(url)

    for limit in stored["users"].values():
        assert UserModify(hwid_limit=limit).hwid_limit == limit
        assert 0 <= node_connection_cap(limit) <= 2**32 - 1
    for limit in stored["user_templates"].values():
        assert UserTemplateModify(hwid_limit=limit).hwid_limit == limit


def test_the_downgrade_to_the_prod_schema_keeps_the_same_device_caps(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'hwid_down.sqlite3'}"
    _run(url, upgrade, SANITIZE_PARENT)
    _seed(url)
    before = _device_policies(monkeypatch, _stored(url))
    _run(url, upgrade, SANITIZE_REVISION)
    sanitized = _stored(url)

    _run(url, downgrade, SANITIZE_PARENT)

    assert _stored(url) == sanitized
    assert _device_policies(monkeypatch, _stored(url)) == before


@pytest.mark.parametrize("limit", [-1, -(2**31)])
def test_a_negative_limit_and_the_sanitized_limit_admit_the_same_devices(monkeypatch, limit):
    assert _device_policy(monkeypatch, limit) == _device_policy(monkeypatch, MAX_HWID_LIMIT)
