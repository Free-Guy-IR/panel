import dataclasses
import logging
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, User
from app.fork.models.proxy_secret_claim import ProxySecretClaim
from app.fork.proxy_secrets import ProxySecretField, enforce_unique_proxy_secrets
from app.fork.proxy_secrets.claims import ProxySecretClaimConflict
from app.fork.proxy_secrets.fields import SPEC_BY_FIELD
from app.fork.proxy_secrets.uniqueness import ProxySecretConflictError
from app.models.proxy import ProxyTable

HY1 = ProxySecretField.hysteria_auth
SUBMITTED_HY1 = frozenset({HY1})
LOGGER = "proxy-secret-uniqueness"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _admin(db):
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    await db.commit()
    return admin


def _settings(auth):
    settings = ProxyTable().dict()
    settings["hysteria"] = {"auth": auth}
    return settings


async def _user(db, admin, auth):
    user = User(username=f"u-{uuid4().hex[:8]}", proxy_settings=_settings(auth), admin_id=admin.id)
    db.add(user)
    await db.commit()
    return user


async def _rotate(db, user, auth):
    user.proxy_settings = _settings(auth)
    await db.commit()


async def _copy_without_claim(db, admin, auth):
    user = await _user(db, admin, f"placeholder-{uuid4().hex}")
    await db.execute(
        update(User.__table__).where(User.__table__.c.id == user.id).values(proxy_settings=_settings(auth))
    )
    await db.commit()
    return user


def _incoming(auth):
    proxy_settings = ProxyTable()
    proxy_settings.hysteria.auth = auth
    return proxy_settings


async def _enforce(db, auth, *, user_id=None, reserved=None):
    return await enforce_unique_proxy_secrets(
        db, _incoming(auth), exclude_user_id=user_id, reserved=reserved, explicit_fields=SUBMITTED_HY1
    )


def _freeze_generator(monkeypatch, *values):
    produced = iter(values)
    monkeypatch.setitem(SPEC_BY_FIELD, HY1, dataclasses.replace(SPEC_BY_FIELD[HY1], generate=lambda: next(produced)))


def _warnings(caplog):
    return [record.getMessage() for record in caplog.records if record.name == LOGGER]


async def _claims_of(db, user):
    table = ProxySecretClaim.__table__
    rows = await db.execute(
        select(table.c.digest, table.c.retired_at).where(table.c.user_id == user.id, table.c.kind == HY1.value)
    )
    return rows.all()


@pytest.mark.asyncio
async def test_a_users_own_retired_value_keeps_its_current_one_before_anything_is_flushed(
    db, replace_submitted_collisions
):
    admin = await _admin(db)
    old, current = f"old-{uuid4().hex}", f"cur-{uuid4().hex}"
    user = await _user(db, admin, old)
    await _rotate(db, user, current)

    prepared = await _enforce(db, old, user_id=user.id)

    assert prepared.hysteria.auth == current
    assert not db.new and not db.dirty


@pytest.mark.asyncio
async def test_refuse_mode_decides_a_users_own_retired_value_itself_not_at_the_flush(db, refuse_submitted_collisions):
    admin = await _admin(db)
    old = f"old-{uuid4().hex}"
    user = await _user(db, admin, old)
    await _rotate(db, user, f"cur-{uuid4().hex}")

    with pytest.raises(ProxySecretConflictError) as refused:
        await _enforce(db, old, user_id=user.id)

    assert not isinstance(refused.value, ProxySecretClaimConflict)
    assert refused.value.fields == [HY1.value]


@pytest.mark.asyncio
async def test_a_kept_current_value_is_logged_without_any_value(db, replace_submitted_collisions, caplog):
    admin = await _admin(db)
    old, current = f"old-{uuid4().hex}", f"cur-{uuid4().hex}"
    user = await _user(db, admin, old)
    await _rotate(db, user, current)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await _enforce(db, old, user_id=user.id)

    assert _warnings(caplog) == [
        "proxy secret hysteria.auth submitted for this user was retired from this user; keeping its current value"
    ]
    assert old not in caplog.text and current not in caplog.text


@pytest.mark.asyncio
async def test_a_regenerated_value_that_collides_again_is_regenerated_not_treated_as_submitted(
    db, replace_submitted_collisions, monkeypatch, caplog
):
    admin = await _admin(db)
    old, current, taken, fresh = (f"{name}-{uuid4().hex}" for name in ("old", "cur", "taken", "fresh"))
    user = await _user(db, admin, old)
    await _rotate(db, user, current)
    await _user(db, admin, taken)
    _freeze_generator(monkeypatch, old, fresh)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        prepared = await _enforce(db, taken, user_id=user.id)

    assert prepared.hysteria.auth == fresh
    assert _warnings(caplog) == [
        (
            "proxy secret hysteria.auth submitted for this user is already stored by 1 other user(s); "
            "replacing it with a freshly generated unique value"
        ),
        "generated proxy secret hysteria.auth collided with a held or used value; regenerating it",
    ]


@pytest.mark.asyncio
async def test_a_regenerated_value_held_by_another_user_is_logged_as_generated(
    db, replace_submitted_collisions, monkeypatch, caplog
):
    admin = await _admin(db)
    taken, again, fresh = (f"{name}-{uuid4().hex}" for name in ("taken", "again", "fresh"))
    await _user(db, admin, taken)
    await _user(db, admin, again)
    _freeze_generator(monkeypatch, again, fresh)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        prepared = await _enforce(db, taken)

    assert prepared.hysteria.auth == fresh
    assert _warnings(caplog)[1:] == [
        "generated proxy secret hysteria.auth collided with a held or used value; regenerating it"
    ]


@pytest.mark.asyncio
async def test_a_look_alike_of_a_live_credential_is_logged_as_held_now(db, replace_submitted_collisions, caplog):
    admin = await _admin(db)
    live = str(uuid4())
    await _user(db, admin, live)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        prepared = await _enforce(db, live.upper())

    assert prepared.hysteria.auth not in (live, live.upper())
    assert _warnings(caplog) == [
        (
            "proxy secret hysteria.auth submitted for this user is held by another user; "
            "replacing it with a freshly generated unique value"
        )
    ]


@pytest.mark.asyncio
async def test_a_value_another_user_held_before_is_logged_as_held_before(db, replace_submitted_collisions, caplog):
    admin = await _admin(db)
    past = f"past-{uuid4().hex}"
    other = await _user(db, admin, past)
    await _rotate(db, other, f"now-{uuid4().hex}")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        prepared = await _enforce(db, past)

    assert prepared.hysteria.auth != past
    assert _warnings(caplog) == [
        (
            "proxy secret hysteria.auth submitted for this user was held by another user before; "
            "replacing it with a freshly generated unique value"
        )
    ]


@pytest.mark.asyncio
async def test_a_value_an_earlier_user_of_the_request_took_is_logged_as_taken_in_the_request(
    db, replace_submitted_collisions, caplog
):
    reserved = {}
    value = f"batch-{uuid4().hex}"
    first = await _enforce(db, value, reserved=reserved)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        second = await _enforce(db, value, reserved=reserved)

    assert first.hysteria.auth == value
    assert second.hysteria.auth != value
    assert _warnings(caplog) == [
        (
            "proxy secret hysteria.auth submitted for this user was already taken by an earlier user in the same "
            "request; replacing it with a freshly generated unique value"
        )
    ]


@pytest.mark.asyncio
async def test_a_legacy_duplicate_rotates_the_claim_holder_that_resubmits_its_own_value(
    db, replace_submitted_collisions
):
    admin = await _admin(db)
    shared = f"legacy-{uuid4().hex}"
    holder = await _user(db, admin, shared)
    copy = await _copy_without_claim(db, admin, shared)

    prepared = await _enforce(db, shared, user_id=holder.id)

    assert prepared.hysteria.auth != shared
    assert (await db.get(User, copy.id, populate_existing=True)).proxy_settings["hysteria"]["auth"] == shared
    assert [retired_at for _, retired_at in await _claims_of(db, holder)] == [None]
