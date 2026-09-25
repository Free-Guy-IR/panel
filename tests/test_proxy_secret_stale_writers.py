from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import get_user_by_id, reset_user_by_next
from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, NextPlan, ProxyInbound, User, UserTemplate
from app.models.core import CoreType
from app.models.group import GroupModify
from app.models.proxy import ProxyTable
from app.operation import OperatorType
from app.operation.group import GroupOperation

WG_TAG = "stale-wg"
VLESS = "stale-vless"
OWNER = SimpleNamespace(username="stale-owner", is_owner=True, role=None)


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stale.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from app import notification
    from app.fork.proxy_secrets import group_repair
    from app.operation import group as group_module

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(group_module, "sync_users", nothing)
    monkeypatch.setattr(group_repair, "sync_users", nothing)
    for name in ("modify_group", "remove_group"):
        monkeypatch.setattr(notification, name, nothing)


async def _seed(sessions):
    async with sessions() as db:
        role = AdminRole(name="stale-owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username="stale-owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        inbounds = {tag: ProxyInbound(tag=tag) for tag in (WG_TAG, VLESS)}
        db.add_all(inbounds.values())
        await db.flush()
        db.add(
            CoreConfig(
                name="stale-wg",
                type=CoreType.wg,
                config={"interface_name": WG_TAG, "address": ["10.91.0.1/24"], "listen_port": 51820},
            )
        )
        bare = Group(name="stale-bare", inbounds=[inbounds[VLESS]])
        db.add(bare)
        await db.flush()
        template = UserTemplate(
            name="stale-plan",
            username_prefix=None,
            username_suffix=None,
            extra_settings={"method": "aes-128-gcm", "flow": None},
            groups=[bare],
            data_limit=5 * 1024**3,
            expire_duration=0,
        )
        db.add(template)
        await db.flush()
        user = User(username="stale-member", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.groups = [bare]
        user.data_limit = 1024**3
        user.used_traffic = 1024**3
        db.add(user)
        await db.flush()
        db.add(NextPlan(user_id=user.id, user_template_id=template.id))
        await db.commit()
        return SimpleNamespace(user_id=user.id, group_id=bare.id)


async def _rotate_auth(sessions, user_id):
    rotated = f"rotated-{uuid4().hex}"
    async with sessions() as db:
        user = await db.get(User, user_id)
        user.proxy_settings = {**user.proxy_settings, "hysteria": {"auth": rotated}}
        await db.commit()
    return rotated


async def _stored_auth(sessions, user_id):
    async with sessions() as db:
        settings = await db.scalar(select(User.proxy_settings).where(User.id == user_id))
    return settings["hysteria"]["auth"]


@pytest.mark.asyncio
async def test_a_next_plan_applied_from_a_copy_loaded_before_a_rotation_keeps_the_rotation(sessions):
    seed = await _seed(sessions)

    async with sessions() as db:
        db_user = await get_user_by_id(db, seed.user_id)
        loaded_auth = db_user.proxy_settings["hysteria"]["auth"]
        await db.commit()
        rotated = await _rotate_auth(sessions, seed.user_id)

        await reset_user_by_next(db, db_user)

    assert loaded_auth != rotated
    assert await _stored_auth(sessions, seed.user_id) == rotated


@pytest.mark.asyncio
async def test_a_group_change_that_allocates_wireguard_keeps_a_rotation_made_meanwhile(sessions, monkeypatch):
    from app.fork.proxy_secrets import group_repair

    seed = await _seed(sessions)
    real = group_repair.allocate_and_grant_with_one_retry
    state = SimpleNamespace(rotated=None)

    async def rotate_then_allocate(db, users, user_ids):
        state.rotated = await _rotate_auth(sessions, seed.user_id)
        return await real(db, users, user_ids)

    monkeypatch.setattr(group_repair, "allocate_and_grant_with_one_retry", rotate_then_allocate)
    operation = GroupOperation(operator_type=OperatorType.API)

    async def accept_tags(tags):
        return None

    operation.check_inbound_tags = accept_tags

    async with sessions() as db:
        await operation.modify_group(
            db, seed.group_id, GroupModify(name="stale-bare", inbound_tags=[VLESS, WG_TAG]), OWNER
        )

    assert state.rotated
    assert await _stored_auth(sessions, seed.user_id) == state.rotated
    async with sessions() as db:
        settings = await db.scalar(select(User.proxy_settings).where(User.id == seed.user_id))
    assert settings["wireguard"]["peer_ips"]
