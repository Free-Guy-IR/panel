from collections import Counter
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.fork.operation import entitled_secrets as driver
from app.fork.proxy_secrets import entitled as entitled_module
from app.fork.proxy_secrets.entitled import EntitledSecretField
from app.models.core import CoreType
from app.models.proxy import ProxyTable, check_l2tp_password
from app.models.user import BulkUserFilter
from app.operation import OperatorType
from app.operation.user import UserOperation

L2TP = "l2tp-de"
VLESS = "vless-in"
KEPT = "AlreadyHasOne1234567"
HELD = "HeldByAnotherUser123"
VMESS_ID = "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"
EDITED_VMESS_ID = "c8c1a7ff-4f2c-4b0d-8e9f-5b1f2a3d4e5f"


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'l2tp.db'}")
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
        calls.append(sorted(user.username for user in users))

    monkeypatch.setattr(user_module, "sync_users", record)
    return calls


def _settings(l2tp=None):
    settings = ProxyTable().dict()
    settings["vmess"] = {"id": VMESS_ID}
    settings["l2tp"] = {"password": l2tp}
    return settings


async def _seed(sessions):
    async with sessions() as db:
        role = AdminRole(name="owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username="owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        inbounds = {tag: ProxyInbound(tag=tag) for tag in (L2TP, VLESS)}
        db.add_all(inbounds.values())
        await db.flush()
        db.add(
            CoreConfig(
                name="l2tp",
                type=CoreType.l2tp,
                config={"inbound_tag": L2TP, "server_addr": "vpn.example.com", "psk": "correct-horse"},
            )
        )
        vpn = Group(name="vpn", inbounds=[inbounds[L2TP]])
        plain = Group(name="plain", inbounds=[inbounds[VLESS]])
        db.add_all([vpn, plain])
        await db.flush()

        rows = {
            "first": (_settings(), [vpn]),
            "second": (_settings(), [vpn]),
            "kept": (_settings(KEPT), [vpn]),
            "holder": (_settings(HELD), [plain]),
            "outsider": (_settings(), [plain]),
        }
        for username, (settings, groups) in rows.items():
            user = User(username=username, proxy_settings=settings, admin_id=admin.id)
            user.groups = groups
            db.add(user)
        await db.commit()


async def _stored(sessions):
    async with sessions() as db:
        rows = (await db.execute(select(User))).scalars().all()
        return {user.username: user.proxy_settings for user in rows}


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


@pytest.mark.asyncio
async def test_the_dry_run_counts_the_entitled_users_without_a_password(sessions, synced):
    await _seed(sessions)
    before = await _stored(sessions)

    async with sessions() as db:
        result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter(dry_run=True))

    assert result.affected_users == 2
    assert await _stored(sessions) == before
    assert synced == []


@pytest.mark.asyncio
async def test_activation_issues_valid_passwords_only_to_entitled_users_without_one(sessions, synced):
    await _seed(sessions)

    async with sessions() as db:
        result = await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 2 users"}
    stored = await _stored(sessions)
    for username in ("first", "second"):
        password = stored[username]["l2tp"]["password"]
        assert check_l2tp_password(password) == password
        assert len(password) == 20 and password.isalnum()
    assert stored["first"]["l2tp"]["password"] != stored["second"]["l2tp"]["password"]
    assert stored["kept"]["l2tp"]["password"] == KEPT
    assert stored["outsider"]["l2tp"]["password"] is None
    assert synced == [["first", "second"]]


@pytest.mark.asyncio
async def test_activation_never_hands_out_a_password_another_user_already_holds(sessions, synced, monkeypatch):
    from app.operation import user as user_module

    await _seed(sessions)
    values = iter([HELD, HELD, "FreshPassword0000001", "FreshPassword0000002", "FreshPassword0000003"])

    def stepping():
        return next(values)

    spec = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.l2tp_password]
    monkeypatch.setitem(
        entitled_module.ENTITLED_SPEC_BY_FIELD, EntitledSecretField.l2tp_password, replace(spec, generate=stepping)
    )
    monkeypatch.setattr(user_module, "generate_l2tp_password", stepping)

    async with sessions() as db:
        await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter())

    stored = await _stored(sessions)
    held = Counter(settings["l2tp"]["password"] for settings in stored.values() if settings["l2tp"]["password"])
    assert held[HELD] == 1
    assert max(held.values()) == 1
    assert stored["first"]["l2tp"]["password"]
    assert stored["second"]["l2tp"]["password"]


@pytest.mark.asyncio
async def test_a_concurrent_edit_of_another_proxy_field_is_preserved(sessions, synced, monkeypatch):
    from app.operation import user as user_module

    await _seed(sessions)
    state = {"edited": False}

    async def edit_first_user():
        if state["edited"]:
            return
        state["edited"] = True
        async with sessions() as other:
            user = (await other.execute(select(User).where(User.username == "first"))).scalar_one()
            user.proxy_settings = {**user.proxy_settings, "vmess": {"id": EDITED_VMESS_ID}}
            await other.commit()

    original_candidates = user_module.get_users_for_l2tp_activation
    original_missing = driver._missing_in_chunk

    async def candidates_then_edit(db, bulk_model):
        users = await original_candidates(db, bulk_model)
        await edit_first_user()
        return users

    async def missing_then_edit(db, spec, user_ids, run):
        missing = await original_missing(db, spec, user_ids, run)
        await edit_first_user()
        return missing

    monkeypatch.setattr(user_module, "get_users_for_l2tp_activation", candidates_then_edit)
    monkeypatch.setattr(driver, "_missing_in_chunk", missing_then_edit)

    async with sessions() as db:
        await _operation().bulk_activate_l2tp_passwords(db, BulkUserFilter())

    stored = await _stored(sessions)
    assert state["edited"] is True
    assert stored["first"]["vmess"]["id"] == EDITED_VMESS_ID
    assert stored["first"]["l2tp"]["password"]


def test_the_l2tp_activation_route_requires_the_users_update_scope():
    from app.fork.routers import user as router_module

    routes = {route.path: route for route in router_module.router.routes}
    l2tp = routes["/api/users/bulk/l2tp_activate"]
    (dependency,) = [dep.call for dep in l2tp.dependant.dependencies if dep.call.__qualname__.endswith("_check")]

    assert l2tp.methods == {"POST"}
    assert dependency.__qualname__ == "require_scope_all.<locals>._check"
    assert sorted(cell.cell_contents for cell in dependency.__closure__) == ["update", "users"]
    assert l2tp.dependant.body_params[0].field_info.annotation is BulkUserFilter
