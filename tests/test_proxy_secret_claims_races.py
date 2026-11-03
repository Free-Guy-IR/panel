import asyncio
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.crud.user import create_user, get_user_by_id, revoke_user_sub
from app.db.models import Admin, AdminRole, Base, User
from app.fork.models.proxy_secret_claim import ProxySecretClaim
from app.fork.proxy_secrets import (
    SECRET_FIELD_SPECS,
    ProxySecretClaimConflict,
    ProxySecretField,
    read_stored_secret,
    supplied_secret_fields,
)
from app.fork.proxy_secrets.claims import secret_digest
from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, is_deadlock, named_lock_seed_statement
from app.models.proxy import ProxyTable
from app.models.user import UserCreate, UserModify
from app.operation import OperatorType
from app.operation.user import UserOperation
from config import database_settings

GITHUB_MODE = os.getenv("TEST_FROM", "local").lower() != "local"
PREFIX = "claimrace-"


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


async def _purge(engine):
    async with engine.begin() as conn:
        user_ids = select(User.id).where(User.username.like(f"{PREFIX}%")).scalar_subquery()
        await conn.execute(delete(ProxySecretClaim.__table__).where(ProxySecretClaim.user_id.in_(user_ids)))
        await conn.execute(delete(User.__table__).where(User.username.like(f"{PREFIX}%")))
        await conn.execute(delete(Admin.__table__).where(Admin.username.like(f"{PREFIX}%")))
        await conn.execute(delete(AdminRole.__table__).where(AdminRole.name.like(f"{PREFIX}%")))


@pytest_asyncio.fixture
async def engine(tmp_path):
    if GITHUB_MODE:
        engine = create_async_engine(database_settings.url, poolclass=NullPool)
        await _purge(engine)
        async with engine.begin() as conn:
            await conn.execute(named_lock_seed_statement(engine.dialect.name, PROXY_SECRETS_LOCK))
        yield engine
        await _purge(engine)
        await engine.dispose()
        return
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'claims.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from app.operation import user as user_module

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(user_module, "sync_users", nothing)
    monkeypatch.setattr(user_module, "sync_user", nothing)


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


async def _admin(sessions):
    async with sessions() as db:
        role = AdminRole(name=f"{PREFIX}{uuid4().hex[:6]}", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username=f"{PREFIX}{uuid4().hex[:6]}", hashed_password="x", role_id=role.id)
        db.add(admin)
        await db.commit()
        return admin.id


async def _holder(sessions, admin_id, auth):
    async with sessions() as db:
        settings = ProxyTable().dict()
        settings["hysteria"] = {"auth": auth}
        user = User(username=f"{PREFIX}{uuid4().hex[:8]}", proxy_settings=settings, admin_id=admin_id)
        db.add(user)
        await db.commit()
        return user.id


async def _create(sessions, admin_id, auth):
    async with sessions() as db:
        db_admin = await db.get(Admin, admin_id)
        new_user = UserCreate(username=f"{PREFIX}{uuid4().hex[:8]}", proxy_settings={"hysteria": {"auth": auth}})
        new_user.proxy_settings = await _operation()._prepare_user_proxy_settings(
            db, [], new_user.proxy_settings, explicit_fields=supplied_secret_fields(new_user.proxy_settings)
        )
        await create_user(db, new_user, [], db_admin)


async def _holders_of(sessions, auth):
    async with sessions() as db:
        rows = (await db.execute(select(User.id, User.proxy_settings).where(User.username.like(f"{PREFIX}%")))).all()
    return sorted(user_id for user_id, settings in rows if (settings.get("hysteria") or {}).get("auth") == auth)


async def _revoke(sessions, user_id):
    async with sessions() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        prepared = (await _operation()._prepare_revoked_users(db, [db_user]))[db_user.id]
        await revoke_user_sub(db, db_user, proxy_settings=prepared)


def _refusals(outcomes):
    return [outcome for outcome in outcomes if isinstance(outcome, HTTPException) and outcome.status_code == 409]


@pytest.mark.asyncio
async def test_two_concurrent_creates_submitting_one_auth_store_it_once(sessions, refuse_submitted_collisions):
    admin_id = await _admin(sessions)
    auth = f"race-{uuid4().hex}"

    outcomes = await asyncio.gather(*(_create(sessions, admin_id, auth) for _ in range(4)), return_exceptions=True)

    assert len([outcome for outcome in outcomes if outcome is None]) == 1, outcomes
    assert len(_refusals(outcomes)) == 3, outcomes
    assert len(await _holders_of(sessions, auth)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["rotation-first", "submission-first", "together"])
async def test_a_rotation_racing_a_submission_of_the_old_auth_never_hands_it_on(
    sessions, refuse_submitted_collisions, order
):
    admin_id = await _admin(sessions)
    auth = f"old-{uuid4().hex}"
    holder_id = await _holder(sessions, admin_id, auth)

    if order == "rotation-first":
        await _revoke(sessions, holder_id)
        outcomes = await asyncio.gather(_create(sessions, admin_id, auth), return_exceptions=True)
    elif order == "submission-first":
        outcomes = await asyncio.gather(_create(sessions, admin_id, auth), return_exceptions=True)
        await _revoke(sessions, holder_id)
    else:
        outcomes = await asyncio.gather(
            _revoke(sessions, holder_id), _create(sessions, admin_id, auth), return_exceptions=True
        )
        outcomes = outcomes[1:]

    assert len(_refusals(outcomes)) == 1, outcomes
    assert await _holders_of(sessions, auth) == []


@pytest.mark.asyncio
async def test_a_writer_that_skips_the_guard_cannot_store_another_users_auth(sessions):
    admin_id = await _admin(sessions)
    auth = f"held-{uuid4().hex}"
    holder_id = await _holder(sessions, admin_id, auth)
    other_id = await _holder(sessions, admin_id, f"own-{uuid4().hex}")

    async with sessions() as db:
        other = await db.get(User, other_id)
        other.proxy_settings = {**other.proxy_settings, "hysteria": {"auth": auth}}
        with pytest.raises(ProxySecretClaimConflict):
            await db.commit()

    assert await _holders_of(sessions, auth) == [holder_id]


@pytest.mark.asyncio
async def test_two_unguarded_writers_racing_for_one_auth_leave_one_holder(sessions):
    admin_id = await _admin(sessions)
    auth = f"contested-{uuid4().hex}"
    first_id = await _holder(sessions, admin_id, f"first-{uuid4().hex}")
    second_id = await _holder(sessions, admin_id, f"second-{uuid4().hex}")

    async def write(user_id):
        async with sessions() as db:
            user = await db.get(User, user_id)
            user.proxy_settings = {**user.proxy_settings, "hysteria": {"auth": auth}}
            await db.commit()

    outcomes = await asyncio.gather(write(first_id), write(second_id), return_exceptions=True)

    refused = [outcome for outcome in outcomes if isinstance(outcome, ProxySecretClaimConflict) or is_deadlock(outcome)]
    assert len(refused) == 1, outcomes
    assert outcomes.count(None) == 1, outcomes
    assert len(await _holders_of(sessions, auth)) == 1


@pytest.mark.asyncio
async def test_a_stale_whole_document_write_cannot_bring_back_a_rotated_auth(sessions):
    admin_id = await _admin(sessions)
    auth = f"leaked-{uuid4().hex}"
    user_id = await _holder(sessions, admin_id, auth)

    async with sessions() as stale:
        stale_user = await stale.get(User, user_id)
        stale_copy = dict(stale_user.proxy_settings)
        await stale.commit()

        await _revoke(sessions, user_id)

        stale_user.proxy_settings = {
            **stale_copy,
            "shadowsocks": {**stale_copy["shadowsocks"], "method": "aes-128-gcm"},
        }
        with pytest.raises(ProxySecretClaimConflict):
            await stale.commit()

    assert await _holders_of(sessions, auth) == []


@pytest.mark.asyncio
async def test_the_duplicate_repair_works_from_the_rows_it_locked(sessions):
    from app.fork.proxy_secrets import BulkRepairProxySecrets

    admin_id = await _admin(sessions)
    shared = f"shared-{uuid4().hex}"
    async with sessions() as db:
        users = [
            User(username=f"{PREFIX}{uuid4().hex[:8]}", proxy_settings=ProxyTable().dict(), admin_id=admin_id)
            for _ in range(2)
        ]
        db.add_all(users)
        await db.commit()
        pair = [user.id for user in users]
        for user in users:
            await db.execute(
                update(User.__table__)
                .where(User.__table__.c.id == user.id)
                .values(proxy_settings={**user.proxy_settings, "hysteria2": {"password": shared}})
            )
        await db.commit()
    rotated_id = pair[0]
    rotated = f"rotated-{uuid4().hex}"

    async with sessions() as db:
        loaded_early = await db.get(User, rotated_id)
        stale_auth = loaded_early.proxy_settings["hysteria"]["auth"]
        await db.commit()

        async with sessions() as other:
            user = await other.get(User, rotated_id)
            user.proxy_settings = {**user.proxy_settings, "hysteria": {"auth": rotated}}
            await other.commit()

        await _operation().bulk_repair_duplicate_proxy_secrets(
            db, BulkRepairProxySecrets(secret_fields={"hysteria2.password"})
        )

    assert stale_auth != rotated
    assert (await _stored(sessions, rotated_id))["hysteria"]["auth"] == rotated
    passwords = {(await _stored(sessions, user_id))["hysteria2"]["password"] for user_id in pair}
    assert shared not in passwords
    assert len(passwords) == 2


async def _stored(sessions, user_id):
    async with sessions() as db:
        return await db.scalar(select(User.proxy_settings).where(User.id == user_id))


OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)


@pytest.fixture
def replace_submitted(monkeypatch, replace_submitted_collisions):
    from app import notification

    async def nothing(*args, **kwargs):
        return None

    for name in ("modify_user", "user_status_change", "user_subscription_revoked"):
        if hasattr(notification, name):
            monkeypatch.setattr(notification, name, nothing)


def _quiet_operation():
    operation = _operation()

    async def no_push(db_user, include_subscription_url=True):
        return SimpleNamespace(username=db_user.username, status=db_user.status)

    operation.update_user = no_push
    operation.validate_user = no_push
    return operation


async def _modify(sessions, user_id, proxy_settings):
    async with sessions() as db:
        db_user = await get_user_by_id(db, user_id, load_admin_role=True)
        await _quiet_operation()._modify_user(
            db, db_user, UserModify(proxy_settings=proxy_settings), OWNER, skip_role_limits=True
        )


async def _create_with(sessions, admin_id, proxy_settings):
    async with sessions() as db:
        db_admin = await db.get(Admin, admin_id)
        new_user = UserCreate(username=f"{PREFIX}{uuid4().hex[:8]}", proxy_settings=proxy_settings)
        new_user.proxy_settings = await _operation()._prepare_user_proxy_settings(
            db, [], new_user.proxy_settings, explicit_fields=supplied_secret_fields(new_user.proxy_settings)
        )
        created = await create_user(db, new_user, [], db_admin)
        return created.id


async def _all_settings(sessions):
    async with sessions() as db:
        rows = (await db.execute(select(User.id, User.proxy_settings).where(User.username.like(f"{PREFIX}%")))).all()
    return dict(rows)


def _assert_no_shared_credential(settings_by_user):
    for protocol, attribute in (
        ("hysteria", "auth"),
        ("hysteria2", "password"),
        ("tuic", "uuid"),
        ("tuic", "password"),
    ):
        values = [
            str(value)
            for settings in settings_by_user.values()
            if (value := (settings.get(protocol) or {}).get(attribute))
        ]
        assert len(values) == len(set(values)), (protocol, attribute, values)


def _held_template(auth, hy2, tuic_uuid, tuic_password):
    return {
        "hysteria": {"auth": auth},
        "hysteria2": {"password": hy2},
        "tuic": {"uuid": tuic_uuid, "password": tuic_password},
    }


@pytest.mark.asyncio
async def test_concurrent_creates_and_modifies_submitting_held_credentials_never_share_one(sessions, replace_submitted):
    admin_id = await _admin(sessions)
    template = _held_template(f"held-{uuid4().hex}", f"hy2-{uuid4().hex}", str(uuid4()), f"tuic-{uuid4().hex}")
    holder_id = await _create_with(sessions, admin_id, template)
    edited_ids = [await _holder(sessions, admin_id, f"own-{uuid4().hex}") for _ in range(3)]
    edited_before = await _all_settings(sessions)

    outcomes = await asyncio.gather(
        *(_create_with(sessions, admin_id, template) for _ in range(4)),
        *(_modify(sessions, user_id, {**edited_before[user_id], **template}) for user_id in edited_ids),
        return_exceptions=True,
    )

    assert not [outcome for outcome in outcomes if isinstance(outcome, BaseException)], outcomes
    stored = await _all_settings(sessions)
    _assert_no_shared_credential(stored)
    assert stored[holder_id]["hysteria"]["auth"] == template["hysteria"]["auth"]
    for user_id in edited_ids:
        assert stored[user_id]["hysteria"]["auth"] != edited_before[user_id]["hysteria"]["auth"]
        assert stored[user_id]["vless"] == edited_before[user_id]["vless"]
    assert await _holders_of(sessions, template["hysteria"]["auth"]) == [holder_id]


@pytest.mark.asyncio
async def test_concurrent_creates_submitting_one_unheld_auth_store_it_exactly_once(sessions, replace_submitted):
    admin_id = await _admin(sessions)
    auth = f"fresh-{uuid4().hex}"

    outcomes = await asyncio.gather(
        *(_create_with(sessions, admin_id, {"hysteria": {"auth": auth}}) for _ in range(5)), return_exceptions=True
    )

    assert not [outcome for outcome in outcomes if isinstance(outcome, BaseException)], outcomes
    assert len(await _holders_of(sessions, auth)) == 1
    _assert_no_shared_credential(await _all_settings(sessions))


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["rotation-first", "stale-write-first", "together"])
async def test_a_stale_edit_racing_a_rotation_never_brings_the_old_credential_back(sessions, replace_submitted, order):
    admin_id = await _admin(sessions)
    old_auth = f"old-{uuid4().hex}"
    user_id = await _holder(sessions, admin_id, old_auth)
    other_id = await _holder(sessions, admin_id, f"other-{uuid4().hex}")
    stale_copy = (await _all_settings(sessions))[user_id]
    other_before = (await _all_settings(sessions))[other_id]

    stale_writes = (
        _modify(sessions, user_id, stale_copy),
        _modify(sessions, other_id, {**other_before, "hysteria": {"auth": old_auth}}),
    )
    rotated = None
    if order == "rotation-first":
        await _revoke(sessions, user_id)
        rotated = (await _all_settings(sessions))[user_id]
        outcomes = await asyncio.gather(*stale_writes, return_exceptions=True)
    elif order == "stale-write-first":
        outcomes = await asyncio.gather(*stale_writes, return_exceptions=True)
        await _revoke(sessions, user_id)
    else:
        outcomes = (await asyncio.gather(_revoke(sessions, user_id), *stale_writes, return_exceptions=True))[1:]

    assert not [outcome for outcome in outcomes if isinstance(outcome, BaseException)], outcomes
    stored = await _all_settings(sessions)
    assert await _holders_of(sessions, old_auth) == []
    assert stored[user_id]["hysteria"]["auth"] != old_auth
    if rotated is not None:
        assert _secrets(stored[user_id]) == _secrets(rotated)
    claims = await _auth_claims(sessions, user_id)
    assert sorted(retired_at is None for _, retired_at in claims) == [False, True], claims
    assert [digest for digest, retired_at in claims if retired_at is None] == [
        secret_digest(ProxySecretField.hysteria_auth, stored[user_id]["hysteria"]["auth"])
    ]
    _assert_no_shared_credential(stored)


def _secrets(settings):
    return {spec.field: read_stored_secret(settings, spec) for spec in SECRET_FIELD_SPECS}


async def _auth_claims(sessions, user_id):
    table = ProxySecretClaim.__table__
    async with sessions() as db:
        rows = await db.execute(
            select(table.c.digest, table.c.retired_at).where(
                table.c.user_id == user_id, table.c.kind == ProxySecretField.hysteria_auth.value
            )
        )
        return rows.all()
