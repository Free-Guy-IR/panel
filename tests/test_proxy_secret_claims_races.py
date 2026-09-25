import asyncio
import os
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
from app.fork.proxy_secrets import ProxySecretClaimConflict, supplied_secret_fields
from app.fork.proxy_secrets.locks import PROXY_SECRETS_LOCK, named_lock_seed_statement
from app.models.proxy import ProxyTable
from app.models.user import UserCreate
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
        prepared = await _operation()._prepare_revoked_proxy_settings(db, db_user)
        await revoke_user_sub(db, db_user, proxy_settings=prepared.dict())


def _refusals(outcomes):
    return [outcome for outcome in outcomes if isinstance(outcome, HTTPException) and outcome.status_code == 409]


@pytest.mark.asyncio
async def test_two_concurrent_creates_submitting_one_auth_store_it_once(sessions):
    admin_id = await _admin(sessions)
    auth = f"race-{uuid4().hex}"

    outcomes = await asyncio.gather(*(_create(sessions, admin_id, auth) for _ in range(4)), return_exceptions=True)

    assert len([outcome for outcome in outcomes if outcome is None]) == 1, outcomes
    assert len(_refusals(outcomes)) == 3, outcomes
    assert len(await _holders_of(sessions, auth)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["rotation-first", "submission-first", "together"])
async def test_a_rotation_racing_a_submission_of_the_old_auth_never_hands_it_on(sessions, order):
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

    assert len([outcome for outcome in outcomes if isinstance(outcome, ProxySecretClaimConflict)]) == 1, outcomes
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

        stale_user.proxy_settings = {**stale_copy, "shadowsocks": {**stale_copy["shadowsocks"], "method": "aes-128-gcm"}}
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
