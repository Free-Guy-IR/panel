from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import get_user_by_id
from app.db.models import Admin, AdminRole, Base, Group, User, UserTemplate
from app.fork.proxy_secrets.locks import acquire_proxy_secrets_lock
from app.models.proxy import ProxyTable
from app.models.settings import HWIDSettings
from app.models.user import BulkUsersApplyTemplate, ModifyUserByTemplate
from app.operation import OperatorType
from app.operation.user import UserOperation

OWNER = SimpleNamespace(username="template-owner", is_owner=True, role=None)
TEMPLATE_METHOD = "aes-256-gcm"
TEMPLATE_DATA_LIMIT = 5 * 1024**3


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'template.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


@pytest.fixture
def operation(monkeypatch):
    from app import notification
    from app.operation import user as user_module

    async def nothing(*args, **kwargs):
        return None

    async def default_hwid():
        return HWIDSettings()

    async def no_node_push(db_user, include_subscription_url=True):
        return SimpleNamespace(username=db_user.username, status=db_user.status, id=db_user.id)

    for name in ("modify_user", "user_status_change", "reset_user_data_usage"):
        monkeypatch.setattr(notification, name, nothing)
    monkeypatch.setattr(user_module, "sync_user", nothing)
    monkeypatch.setattr(user_module, "sync_users", nothing)
    monkeypatch.setattr(user_module, "hwid_settings", default_hwid)
    users = UserOperation.__new__(UserOperation)
    users.operator_type = OperatorType.API
    users.update_user = no_node_push
    users.validate_user = no_node_push
    return users


async def _seed(sessions, *, reset_usages):
    async with sessions() as db:
        role = AdminRole(name="template-owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username="template-owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        group = Group(name="template-group", inbounds=[])
        db.add(group)
        await db.flush()
        template = UserTemplate(
            name="template",
            username_prefix=None,
            username_suffix=None,
            extra_settings={"method": TEMPLATE_METHOD},
            groups=[group],
            data_limit=TEMPLATE_DATA_LIMIT,
            reset_usages=reset_usages,
        )
        user = User(username="template-user", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.used_traffic = 12345
        db.add_all([template, user])
        await db.commit()
        return SimpleNamespace(template_id=template.id, user_id=user.id, admin_id=admin.id)


async def _rotate_hysteria_under_the_lock(sessions, user_id) -> str:
    value = f"rotated-{uuid4().hex}"
    async with sessions() as db:
        await acquire_proxy_secrets_lock(db)
        db_user = await db.get(User, user_id)
        db_user.proxy_settings = {**db_user.proxy_settings, "hysteria": {"auth": value}}
        await db.commit()
    return value


async def _stored(sessions, user_id):
    async with sessions() as db:
        row = (
            await db.execute(select(User.proxy_settings, User.data_limit, User.used_traffic).where(User.id == user_id))
        ).one()
    return SimpleNamespace(
        hysteria=row.proxy_settings["hysteria"]["auth"],
        method=row.proxy_settings["shadowsocks"]["method"],
        data_limit=row.data_limit,
        used_traffic=row.used_traffic,
    )


def _proxy_settings_writes(engine):
    held = {}
    writes = []

    def key(conn):
        return id(conn.connection.dbapi_connection)

    def statement(conn, cursor, text, *rest):
        normalized = " ".join(text.split()).lower()
        if normalized.startswith("update named_locks"):
            held[key(conn)] = True
        elif normalized.startswith("update users set") and "proxy_settings" in normalized.split(" where ", 1)[0]:
            writes.append(held.get(key(conn), False))

    def ended(conn):
        held[key(conn)] = False

    event.listen(engine.sync_engine, "before_cursor_execute", statement)
    event.listen(engine.sync_engine, "commit", ended)
    event.listen(engine.sync_engine, "rollback", ended)
    return writes


async def _apply_template(operation, sessions, seed, db_user=None):
    async with sessions() as db:
        if db_user is None:
            db_user = await get_user_by_id(db, seed.user_id, load_admin_role=True)
        else:
            db_user = await db.merge(db_user, load=False)
        await operation._modify_user_with_template(
            db, db_user, ModifyUserByTemplate(user_template_id=seed.template_id), OWNER
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_usages", [True, False], ids=["reset-usages", "keep-usages"])
async def test_a_template_writes_proxy_settings_only_while_it_holds_the_secrets_lock(
    engine, sessions, operation, reset_usages
):
    seed = await _seed(sessions, reset_usages=reset_usages)
    writes = _proxy_settings_writes(engine)

    await _apply_template(operation, sessions, seed)

    stored = await _stored(sessions, seed.user_id)
    assert stored.method == TEMPLATE_METHOD
    assert writes, "the template never wrote proxy_settings"
    assert all(writes), f"proxy_settings written without the secrets lock: {writes}"


@pytest.mark.asyncio
async def test_a_rotation_committed_after_the_usage_reset_survives_the_template(
    engine, sessions, operation, monkeypatch
):
    from app.operation import user as user_module

    seed = await _seed(sessions, reset_usages=True)
    real_reset = user_module.reset_user_data_usage
    rotated = []

    async def reset_then_rotate(*args, **kwargs):
        result = await real_reset(*args, **kwargs)
        rotated.append(await _rotate_hysteria_under_the_lock(sessions, seed.user_id))
        return result

    monkeypatch.setattr(user_module, "reset_user_data_usage", reset_then_rotate)

    await _apply_template(operation, sessions, seed)

    stored = await _stored(sessions, seed.user_id)
    assert rotated
    assert stored.hysteria == rotated[0]
    assert stored.method == TEMPLATE_METHOD
    assert stored.data_limit == TEMPLATE_DATA_LIMIT
    assert stored.used_traffic == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_usages", [True, False], ids=["reset-usages", "keep-usages"])
async def test_a_rotation_committed_after_the_user_was_loaded_survives_the_template(
    engine, sessions, operation, reset_usages
):
    seed = await _seed(sessions, reset_usages=reset_usages)
    async with sessions() as db:
        stale = await get_user_by_id(db, seed.user_id, load_admin_role=True)
    rotated = await _rotate_hysteria_under_the_lock(sessions, seed.user_id)

    await _apply_template(operation, sessions, seed, db_user=stale)

    stored = await _stored(sessions, seed.user_id)
    assert stored.hysteria == rotated
    assert stored.method == TEMPLATE_METHOD
    assert stored.data_limit == TEMPLATE_DATA_LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("reset_usages", [True, False], ids=["reset-usages", "keep-usages"])
async def test_a_rotation_committed_after_a_bulk_template_loaded_its_users_survives_it(
    engine, sessions, operation, monkeypatch, reset_usages
):
    seed = await _seed(sessions, reset_usages=reset_usages)
    real_load = UserOperation._get_validated_users_by_ids
    rotated = []

    async def load_then_rotate(self, *args, **kwargs):
        loaded = await real_load(self, *args, **kwargs)
        rotated.append(await _rotate_hysteria_under_the_lock(sessions, seed.user_id))
        return loaded

    monkeypatch.setattr(UserOperation, "_get_validated_users_by_ids", load_then_rotate)
    writes = _proxy_settings_writes(engine)

    async with sessions() as db:
        await operation.bulk_apply_template_to_users(
            db, BulkUsersApplyTemplate(ids=[seed.user_id], user_template_id=seed.template_id), OWNER
        )

    stored = await _stored(sessions, seed.user_id)
    assert rotated
    assert stored.hysteria == rotated[0]
    assert stored.method == TEMPLATE_METHOD
    assert stored.data_limit == TEMPLATE_DATA_LIMIT
    assert all(writes), f"proxy_settings written without the secrets lock: {writes}"
