import logging
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User
from app.fork.operation import entitled_secrets as driver
from app.fork.proxy_secrets import ProxySecretUniquenessError
from app.fork.proxy_secrets.entitled import EntitledSecretField
from app.models.core import CoreCreate, CoreType
from app.models.proxy import ProxyTable
from app.operation import OperatorType
from app.operation.core import CoreOperation

WAITING_TAG = "ovpn-new"
EXISTING_TAG = "ovpn-a"
L2TP_TAG = "l2tp-de"
MTP_TAG = "mtp-443"
VLESS = "vless-in"
KEPT = "kept-openvpn-password"


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db(engine):
    async with async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)() as session:
        yield session


@pytest.fixture
def quiet_core(monkeypatch):
    from app import notification
    from app.operation import core as core_module

    synced = []

    async def update_core(*args, **kwargs):
        return None

    async def no_notification(*args, **kwargs):
        return None

    async def record_sync(users):
        synced.append(sorted(user.username for user in users))

    monkeypatch.setattr(
        core_module,
        "core_manager",
        SimpleNamespace(validate_core=lambda *args, **kwargs: object(), update_core=update_core),
    )
    monkeypatch.setattr(notification, "create_core", no_notification)
    monkeypatch.setattr(notification, "modify_core", no_notification)
    monkeypatch.setattr(driver, "sync_users", record_sync)
    return synced


def _operation():
    operation = CoreOperation(operator_type=OperatorType.API)

    async def no_refresh(db):
        return None

    operation._refresh_hosts_from_db = no_refresh
    return operation


ADMIN = SimpleNamespace(username="owner", is_owner=True)


async def _seed(db):
    role = AdminRole(name="owner", is_owner=True)
    db.add(role)
    await db.flush()
    admin = Admin(username="owner", hashed_password="x", role_id=role.id)
    db.add(admin)
    inbounds = {tag: ProxyInbound(tag=tag) for tag in (WAITING_TAG, EXISTING_TAG, L2TP_TAG, MTP_TAG, VLESS)}
    db.add_all(inbounds.values())
    await db.flush()
    existing = CoreConfig(name="ovpn", type=CoreType.openvpn, config={"instances": [{"tag": EXISTING_TAG}]})
    db.add(existing)
    waiting = Group(name="waiting", inbounds=[inbounds[WAITING_TAG], inbounds[L2TP_TAG], inbounds[MTP_TAG]])
    plain = Group(name="plain", inbounds=[inbounds[VLESS]])
    db.add_all([waiting, plain])
    await db.flush()

    for username, password, group in (
        ("newcomer", None, waiting),
        ("second", None, waiting),
        ("holder", KEPT, waiting),
        ("outsider", None, plain),
    ):
        settings = ProxyTable().dict()
        settings["openvpn"] = {"password": password}
        user = User(username=username, proxy_settings=settings, admin_id=admin.id)
        user.groups = [group]
        db.add(user)
    await db.commit()
    return existing


async def _stored(engine):
    async with async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)() as session:
        rows = (await session.execute(select(User))).scalars().all()
        return {user.username: ProxyTable.model_validate(user.proxy_settings) for user in rows}


@pytest.mark.asyncio
async def test_creating_an_openvpn_core_for_a_tag_groups_already_carry_issues_the_passwords(engine, db, quiet_core):
    await _seed(db)

    await _operation().create_core(
        db,
        CoreCreate(name="ovpn-2", type=CoreType.openvpn, config={"instances": [{"tag": WAITING_TAG}]}),
        ADMIN,
    )

    stored = await _stored(engine)
    assert stored["newcomer"].openvpn.password
    assert stored["second"].openvpn.password
    assert stored["newcomer"].openvpn.password != stored["second"].openvpn.password
    assert stored["holder"].openvpn.password == KEPT
    assert stored["outsider"].openvpn.password is None
    assert stored["newcomer"].l2tp.password is None
    assert stored["newcomer"].mtproto.secret is None
    assert quiet_core == [["newcomer", "second"]]


@pytest.mark.asyncio
async def test_widening_an_openvpn_core_to_a_carried_tag_issues_the_passwords(engine, db, quiet_core):
    existing = await _seed(db)

    await _operation().modify_core(
        db,
        existing.id,
        CoreCreate(
            name="ovpn", type=CoreType.openvpn, config={"instances": [{"tag": EXISTING_TAG}, {"tag": WAITING_TAG}]}
        ),
        ADMIN,
    )

    stored = await _stored(engine)
    assert stored["newcomer"].openvpn.password
    assert stored["second"].openvpn.password
    assert stored["holder"].openvpn.password == KEPT
    assert stored["outsider"].openvpn.password is None


@pytest.mark.asyncio
async def test_saving_the_same_core_again_changes_nothing(engine, db, quiet_core):
    existing = await _seed(db)
    change = CoreCreate(
        name="ovpn", type=CoreType.openvpn, config={"instances": [{"tag": EXISTING_TAG}, {"tag": WAITING_TAG}]}
    )
    await _operation().modify_core(db, existing.id, change, ADMIN)
    first = await _stored(engine)

    await _operation().modify_core(db, existing.id, change, ADMIN)

    assert await _stored(engine) == first
    assert len(quiet_core) == 1


@pytest.mark.asyncio
async def test_l2tp_and_mtproto_cores_issue_only_their_own_secret(engine, db, quiet_core):
    await _seed(db)

    await _operation().create_core(
        db,
        CoreCreate(name="l2tp", type=CoreType.l2tp, config={"inbound_tag": L2TP_TAG, "server_addr": "vpn.example.com"}),
        ADMIN,
    )
    after_l2tp = await _stored(engine)
    await _operation().create_core(
        db, CoreCreate(name="mtproto", type=CoreType.mtproto, config={"instances": [{"tag": MTP_TAG}]}), ADMIN
    )
    after_mtproto = await _stored(engine)

    assert after_l2tp["newcomer"].l2tp.password
    assert after_l2tp["newcomer"].mtproto.secret is None
    assert after_l2tp["newcomer"].openvpn.password is None
    assert after_mtproto["newcomer"].mtproto.secret
    assert after_mtproto["newcomer"].l2tp.password == after_l2tp["newcomer"].l2tp.password
    assert after_mtproto["outsider"].l2tp.password is None
    assert after_mtproto["outsider"].mtproto.secret is None


@pytest.mark.asyncio
async def test_a_core_of_another_type_issues_nothing(engine, db, quiet_core):
    await _seed(db)
    before = await _stored(engine)

    await _operation().create_core(
        db, CoreCreate(name="xray", type=CoreType.xray, config={"inbounds": [{"tag": WAITING_TAG}]}), ADMIN
    )

    assert await _stored(engine) == before
    assert quiet_core == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "modify"])
@pytest.mark.parametrize(
    "failure",
    [
        ProxySecretUniquenessError([EntitledSecretField.openvpn_password]),
        OperationalError("UPDATE users", {}, Exception("database is locked")),
    ],
    ids=["uniqueness-exhausted", "database-error"],
)
async def test_a_failed_issue_runs_last_is_logged_at_error_and_fails_the_request_with_an_accurate_message(
    engine, db, quiet_core, monkeypatch, failure, action
):
    from app import notification

    existing = await _seed(db)
    steps = []

    async def failing(*args, **kwargs):
        steps.append("grant")
        raise failure

    async def no_op():
        return None

    def record_notification(name):
        def notify(core, admin_username):
            steps.append(f"notification:{name}:{core.id}")
            return no_op()

        return notify

    monkeypatch.setattr(driver, "issue_unique_secrets", failing)
    monkeypatch.setattr(notification, "create_core", record_notification("create"))
    monkeypatch.setattr(notification, "modify_core", record_notification("modify"))
    operation = _operation()

    async def record_refresh(session):
        steps.append("refresh_hosts")

    operation._refresh_hosts_from_db = record_refresh
    records = []
    handler = logging.Handler(level=logging.ERROR)
    handler.emit = records.append
    logger = logging.getLogger("entitled-secrets")
    logger.addHandler(handler)
    try:
        with pytest.raises(HTTPException) as raised:
            if action == "create":
                await operation.create_core(
                    db,
                    CoreCreate(name="ovpn-2", type=CoreType.openvpn, config={"instances": [{"tag": WAITING_TAG}]}),
                    ADMIN,
                )
            else:
                await operation.modify_core(
                    db,
                    existing.id,
                    CoreCreate(
                        name="ovpn",
                        type=CoreType.openvpn,
                        config={"instances": [{"tag": EXISTING_TAG}, {"tag": WAITING_TAG}]},
                    ),
                    ADMIN,
                )
    finally:
        logger.removeHandler(handler)

    saved_name = "ovpn-2" if action == "create" else "ovpn"
    saved = (await db.execute(select(CoreConfig).where(CoreConfig.name == saved_name))).scalar_one()
    assert steps == [f"notification:{action}:{saved.id}", "refresh_hosts", "grant"]
    assert raised.value.status_code == 500
    assert raised.value.detail.startswith(f"Core {saved.id} is saved and live, so do not create it again.")
    assert "POST /api/users/bulk/openvpn_activate" in raised.value.detail
    errors = [record.getMessage() for record in records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    assert f"core {saved.id} (OpenVPN)" in errors[0]
    assert "issued 0 of the 2 missing" in errors[0]
    assert "among 3 entitled user(s)" in errors[0]
    if action == "modify":
        assert {"tag": WAITING_TAG} in saved.config["instances"]
    stored = await _stored(engine)
    assert stored["newcomer"].openvpn.password is None
