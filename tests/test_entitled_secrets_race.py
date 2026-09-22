import os

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import (
    Admin,
    AdminRole,
    Base,
    CoreConfig,
    Group,
    ProxyInbound,
    User,
    inbounds_groups_association,
    users_groups_association,
)
from app.fork.operation import entitled_secrets as driver
from app.fork.proxy_secrets import entitled as entitled_module
from app.models.core import CoreType
from app.models.proxy import ProxyTable
from app.models.user import BulkUserFilter
from app.operation import OperatorType
from app.operation.user import UserOperation

OVPN = "ovpn-udp"
MTP = "mtp-443"


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


RACE_DB_URL = os.environ.get("ENTITLED_SECRETS_RACE_DB_URL")
PREFIX = "race-"
USERNAME = "racer"


async def _purge(engine):
    async with engine.begin() as conn:
        user_ids = select(User.id).where(User.username == USERNAME).scalar_subquery()
        group_ids = select(Group.id).where(Group.name.like(f"{PREFIX}%")).scalar_subquery()
        await conn.execute(delete(users_groups_association).where(users_groups_association.c.user_id.in_(user_ids)))
        await conn.execute(delete(User.__table__).where(User.username == USERNAME))
        await conn.execute(
            delete(inbounds_groups_association).where(inbounds_groups_association.c.group_id.in_(group_ids))
        )
        await conn.execute(delete(Group.__table__).where(Group.name.like(f"{PREFIX}%")))
        await conn.execute(delete(ProxyInbound.__table__).where(ProxyInbound.tag.in_([OVPN, MTP])))
        await conn.execute(delete(CoreConfig.__table__).where(CoreConfig.name.like(f"{PREFIX}%")))
        await conn.execute(delete(Admin.__table__).where(Admin.username.like(f"{PREFIX}%")))
        await conn.execute(delete(AdminRole.__table__).where(AdminRole.name.like(f"{PREFIX}%")))


@pytest_asyncio.fixture
async def engine(tmp_path):
    if RACE_DB_URL:
        engine = create_async_engine(RACE_DB_URL, poolclass=NullPool)
        await _purge(engine)
        yield engine
        await _purge(engine)
        await engine.dispose()
        return
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


@pytest.fixture
def synced(monkeypatch):
    from app.operation import user as user_module

    calls = []

    async def record(users):
        for user in users:
            calls.append((user.username, dict(user.proxy_settings)))

    monkeypatch.setattr(user_module, "sync_users", record)
    return calls


async def _seed(sessions, extra=None):
    async with sessions() as db:
        role = AdminRole(name=f"{PREFIX}owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username=f"{PREFIX}owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        inbounds = [ProxyInbound(tag=OVPN), ProxyInbound(tag=MTP)]
        db.add_all(inbounds)
        await db.flush()
        db.add_all(
            [
                CoreConfig(name=f"{PREFIX}ovpn", type=CoreType.openvpn, config={"instances": [{"tag": OVPN}]}),
                CoreConfig(name=f"{PREFIX}mtproto", type=CoreType.mtproto, config={"instances": [{"tag": MTP}]}),
            ]
        )
        group = Group(name=f"{PREFIX}vpn", inbounds=inbounds)
        db.add(group)
        await db.flush()
        settings = ProxyTable().dict()
        settings["openvpn"] = {"password": None}
        settings["mtproto"] = {"secret": None}
        settings.update(extra or {})
        user = User(username=USERNAME, proxy_settings=settings, admin_id=admin.id)
        user.groups = [group]
        db.add(user)
        await db.commit()
        return user.id


async def _stored(sessions):
    async with sessions() as db:
        user = (await db.execute(select(User).where(User.username == USERNAME))).scalar_one()
        return user.proxy_settings


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


def _interleave(monkeypatch, module, sessions, second):
    original = module.issue_unique_secrets
    state = {"ran": False}

    async def issue_then_let_the_other_session_commit(db, spec, count, reserved=None):
        values = await original(db, spec, count, reserved)
        if not state["ran"]:
            state["ran"] = True
            async with sessions() as other:
                await second(other)
        return values

    monkeypatch.setattr(module, "issue_unique_secrets", issue_then_let_the_other_session_commit)
    return state


def _last_synced(synced, key, attribute):
    values = [settings[key][attribute] for username, settings in synced if username == USERNAME]
    return values[-1] if values else None


@pytest.mark.asyncio
async def test_a_concurrent_activation_of_another_protocol_is_not_overwritten(sessions, synced, monkeypatch):
    await _seed(sessions)

    async def mtproto_activation(other):
        await _operation().bulk_activate_mtproto_secrets(other, BulkUserFilter())

    state = _interleave(monkeypatch, driver, sessions, mtproto_activation)

    async with sessions() as first:
        await _operation().bulk_activate_openvpn_passwords(first, BulkUserFilter())

    assert state["ran"] is True
    stored = await _stored(sessions)
    assert stored["mtproto"]["secret"]
    assert stored["openvpn"]["password"]
    assert stored["mtproto"]["secret"] == _last_synced(synced, "mtproto", "secret")
    assert stored["openvpn"]["password"] == _last_synced(synced, "openvpn", "password")


@pytest.mark.asyncio
async def test_a_secret_issued_meanwhile_for_the_same_protocol_is_never_rotated(sessions, synced, monkeypatch):
    await _seed(sessions)
    issued_by_second = {}

    async def same_protocol_activation(other):
        await _operation().bulk_activate_openvpn_passwords(other, BulkUserFilter())
        user = (await other.execute(select(User).where(User.username == USERNAME))).scalar_one()
        issued_by_second["password"] = user.proxy_settings["openvpn"]["password"]

    _interleave(monkeypatch, driver, sessions, same_protocol_activation)

    async with sessions() as first:
        result = await _operation().bulk_activate_openvpn_passwords(first, BulkUserFilter())

    stored = await _stored(sessions)
    assert issued_by_second["password"]
    assert stored["openvpn"]["password"] == issued_by_second["password"]
    assert result == {"detail": "operation has been successfuly done on 0 users"}


@pytest.mark.asyncio
async def test_a_group_change_grant_does_not_overwrite_a_secret_committed_meanwhile(sessions, synced, monkeypatch):
    user_id = await _seed(sessions, extra={"future_key": {"kept": True}})

    async def mtproto_activation(other):
        await _operation().bulk_activate_mtproto_secrets(other, BulkUserFilter())

    _interleave(monkeypatch, entitled_module, sessions, mtproto_activation)

    async with sessions() as first:
        users = list((await first.execute(select(User).where(User.id == user_id))).scalars().all())
        changed = await entitled_module.grant_entitled_secrets(first, users)
        await first.commit()
        in_memory = dict(users[0].proxy_settings)

    stored = await _stored(sessions)
    assert stored["mtproto"]["secret"] == _last_synced(synced, "mtproto", "secret")
    assert stored["openvpn"]["password"]
    assert stored["future_key"] == {"kept": True}
    assert [user.id for user in changed] == [user_id]
    assert in_memory == stored


@pytest.mark.asyncio
async def test_a_poisoned_l2tp_value_is_only_replaced_while_it_is_still_the_poisoned_value(sessions):
    from app.fork.proxy_secrets.entitled import EntitledSecretField, put_entitled_secret

    user_id = await _seed(sessions, extra={"l2tp": {"password": "bad\nvalue"}})
    spec = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.l2tp_password]

    async with sessions() as db:
        stale = await put_entitled_secret(db, spec, user_id, "something-else", "NewPassw0rd12345678")
        fresh = await put_entitled_secret(db, spec, user_id, "bad\nvalue", "NewPassw0rd12345678")
        await db.commit()

    assert (stale, fresh) == (False, True)
    stored = await _stored(sessions)
    assert stored["l2tp"] == {"password": "NewPassw0rd12345678"}


@pytest.mark.asyncio
async def test_a_missing_or_null_section_is_created_and_a_present_value_is_left_alone(sessions):
    from app.fork.proxy_secrets.entitled import EntitledSecretField, put_entitled_secret

    user_id = await _seed(sessions)
    async with sessions() as db:
        user = await db.get(User, user_id)
        user.proxy_settings = {key: value for key, value in user.proxy_settings.items() if key != "openvpn"} | {
            "mtproto": None
        }
        await db.commit()

    openvpn = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.openvpn_password]
    mtproto = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.mtproto_secret]
    async with sessions() as db:
        created = await put_entitled_secret(db, openvpn, user_id, None, "first-password")
        from_null = await put_entitled_secret(db, mtproto, user_id, None, "a" * 32)
        again = await put_entitled_secret(db, openvpn, user_id, None, "second-password")
        await db.commit()

    assert (created, from_null, again) == (True, True, False)
    stored = await _stored(sessions)
    assert stored["openvpn"] == {"password": "first-password"}
    assert stored["mtproto"] == {"secret": "a" * 32}
    assert stored["vmess"]


@pytest.mark.asyncio
async def test_a_group_change_whose_grants_are_all_skipped_syncs_what_the_other_session_committed(
    sessions, synced, monkeypatch
):
    from app.operation import group as group_module
    from app.operation.group import GroupOperation

    user_id = await _seed(sessions)
    committed = {}

    async def both_activations(other):
        await _operation().bulk_activate_openvpn_passwords(other, BulkUserFilter())
        await _operation().bulk_activate_mtproto_secrets(other, BulkUserFilter())
        user = (await other.execute(select(User).where(User.id == user_id))).scalar_one()
        committed.update(user.proxy_settings)

    _interleave(monkeypatch, entitled_module, sessions, both_activations)
    pushed = []

    async def record_push(users):
        pushed.extend(dict(user.proxy_settings) for user in users)

    monkeypatch.setattr(group_module, "sync_users", record_push)

    async with sessions() as first:
        users = list((await first.execute(select(User).where(User.id == user_id))).scalars().all())
        await GroupOperation(operator_type=OperatorType.API)._sync_users_allocations(first, users)
        await first.commit()
        await group_module.sync_users(users)

    stored = await _stored(sessions)
    assert committed["openvpn"]["password"]
    assert committed["mtproto"]["secret"]
    assert stored["openvpn"] == committed["openvpn"]
    assert stored["mtproto"] == committed["mtproto"]
    assert len(pushed) == 1
    assert pushed[0]["openvpn"] == committed["openvpn"]
    assert pushed[0]["mtproto"] == committed["mtproto"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_value", "expected_value"),
    [("bad value ", "bad value"), ("bad value", "bad value ")],
    ids=["stored-has-trailing-space", "expected-has-trailing-space"],
)
async def test_the_old_value_guard_is_byte_exact_about_trailing_spaces(sessions, stored_value, expected_value):
    from app.fork.proxy_secrets.entitled import EntitledSecretField, put_entitled_secret

    user_id = await _seed(sessions, extra={"l2tp": {"password": stored_value}})
    spec = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.l2tp_password]

    async with sessions() as db:
        near_miss = await put_entitled_secret(db, spec, user_id, expected_value, "NewPassw0rd12345678")
        await db.commit()
    after_near_miss = await _stored(sessions)
    async with sessions() as db:
        exact = await put_entitled_secret(db, spec, user_id, stored_value, "NewPassw0rd12345678")
        await db.commit()

    assert near_miss is False
    assert after_near_miss["l2tp"] == {"password": stored_value}
    assert exact is True
    assert (await _stored(sessions))["l2tp"] == {"password": "NewPassw0rd12345678"}


def _compiled(dialect_name, expected):
    from sqlalchemy.dialects import mysql, postgresql

    from app.fork.proxy_secrets.entitled import EntitledSecretField, conditional_secret_update

    dialect = {"mysql": mysql.dialect(), "postgresql": postgresql.dialect()}[dialect_name]
    spec = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.openvpn_password]
    statement = conditional_secret_update(dialect_name, spec, 7, expected, "fresh")
    compiled = statement.compile(dialect=dialect)
    return str(compiled), compiled.params


def test_the_mysql_statement_sets_one_key_and_is_guarded():
    sql, params = _compiled("mysql", None)
    lowered = sql.lower()

    assert lowered.startswith("update users set proxy_settings=case when (json_type(json_extract(users.proxy_settings")
    assert "then json_set(users.proxy_settings" in lowered
    assert "else json_set(users.proxy_settings, %s, json_object(%s, %s))" in lowered
    assert "where users.id = %s and coalesce(json_type(json_extract(users.proxy_settings, %s)), %s) = %s" in lowered
    assert {"$.openvpn", "$.openvpn.password", "OBJECT", "NULL", "fresh", 7} <= set(params.values())

    guarded, guarded_params = _compiled("mysql", "old-value")
    assert (
        "cast(json_unquote(json_extract(users.proxy_settings, %s)) as binary) = cast(%s as binary)" in guarded.lower()
    )
    assert {"STRING", "old-value"} <= set(guarded_params.values())


def test_the_postgresql_statement_sets_one_key_and_is_guarded():
    sql, params = _compiled("postgresql", None)

    assert sql.startswith(
        "UPDATE users SET proxy_settings=CAST(CAST(users.proxy_settings AS JSONB) || jsonb_build_object("
    )
    assert "jsonb_build_object(" in sql
    assert "jsonb_typeof(" in sql
    assert sql.count("AS JSON)") == 1
    assert "WHERE users.id = " in sql
    assert {"openvpn", "password", "fresh"} <= set(params.values())

    guarded, guarded_params = _compiled("postgresql", "old-value")
    assert "->>" in guarded
    assert "old-value" in guarded_params.values()


def test_an_unknown_dialect_is_refused():
    from app.fork.proxy_secrets.entitled import EntitledSecretField, conditional_secret_update

    spec = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.openvpn_password]
    with pytest.raises(NotImplementedError):
        conditional_secret_update("oracle", spec, 7, None, "fresh")
