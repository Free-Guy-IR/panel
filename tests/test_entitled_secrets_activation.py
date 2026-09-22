from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, AdminRole, Base, CoreConfig, Group, ProxyInbound, User, UserStatus
from app.fork.operation import entitled_secrets as driver
from app.fork.proxy_secrets import ProxySecretUniquenessError, entitled as entitled_module
from app.fork.proxy_secrets.entitled import EntitledSecretField, issue_unique_secrets
from app.models.core import CoreType
from app.models.proxy import ProxyTable
from app.models.user import BulkUserFilter
from app.operation import OperatorType
from app.operation.user import UserOperation

OVPN = "ovpn-udp"
L2TP = "l2tp-de"
MTP = "mtp-443"
VLESS = "vless-in"
KEPT_OVPN = "kept-openvpn-password"
KEPT_MTP = "0123456789abcdef0123456789abcdef"
VMESS_ID = "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"


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
def synced(monkeypatch):
    from app.operation import user as user_module

    calls = []

    async def fake_sync(users):
        calls.append(sorted(user.username for user in users))

    monkeypatch.setattr(user_module, "sync_users", fake_sync)
    return calls


def _settings(openvpn=None, mtproto=None, extra=None):
    settings = ProxyTable().dict()
    settings["openvpn"] = {"password": openvpn}
    settings["mtproto"] = {"secret": mtproto}
    settings.update(extra or {})
    return settings


async def _seed(db):
    role = AdminRole(name="owner", is_owner=True)
    db.add(role)
    await db.flush()
    admin = Admin(username="owner", hashed_password="x", role_id=role.id)
    db.add(admin)
    inbounds = {tag: ProxyInbound(tag=tag) for tag in (OVPN, L2TP, MTP, VLESS)}
    db.add_all(inbounds.values())
    await db.flush()
    db.add_all(
        [
            CoreConfig(name="ovpn", type=CoreType.openvpn, config={"instances": [{"tag": OVPN}]}),
            CoreConfig(name="mtproto", type=CoreType.mtproto, config={"instances": [{"tag": MTP}]}),
        ]
    )
    vpn = Group(name="vpn", inbounds=[inbounds[OVPN], inbounds[MTP]])
    plain = Group(name="plain", inbounds=[inbounds[VLESS]])
    off = Group(name="off", inbounds=[inbounds[OVPN], inbounds[MTP]], is_disabled=True)
    db.add_all([vpn, plain, off])
    await db.flush()

    rows = {
        "entitled": (_settings(extra={"vmess": {"id": VMESS_ID}, "future_key": {"kept": True}}), [vpn], None),
        "expired": (_settings(), [vpn], UserStatus.expired),
        "holder": (_settings(openvpn=KEPT_OVPN, mtproto=KEPT_MTP), [vpn], None),
        "outsider": (_settings(), [plain], None),
        "switched_off": (_settings(), [off], None),
        "no_groups": (_settings(), [], None),
    }
    ids = {}
    for username, (settings, groups, status) in rows.items():
        user = User(username=username, proxy_settings=settings, admin_id=admin.id)
        if status is not None:
            user.status = status
        user.groups = groups
        db.add(user)
        await db.flush()
        ids[username] = user.id
    await db.commit()
    return ids


async def _stored(engine):
    async with async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)() as session:
        rows = (await session.execute(select(User))).scalars().all()
        return {user.username: user.proxy_settings for user in rows}


def _operation():
    operation = UserOperation.__new__(UserOperation)
    operation.operator_type = OperatorType.API
    return operation


@pytest.mark.asyncio
async def test_the_openvpn_dry_run_counts_only_entitled_users_without_a_password_and_writes_nothing(engine, db, synced):
    await _seed(db)
    before = await _stored(engine)
    writes = []
    event.listen(engine.sync_engine, "commit", lambda conn: writes.append("commit"))
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, *rest: (
            writes.append(statement) if not statement.lstrip().upper().startswith("SELECT") else None
        ),
    )

    result = await _operation().bulk_activate_openvpn_passwords(db, BulkUserFilter(dry_run=True))

    assert result.dry_run is True
    assert result.affected_users == 2
    assert writes == []
    assert synced == []
    assert await _stored(engine) == before


@pytest.mark.asyncio
async def test_the_openvpn_activation_fills_only_entitled_users_and_never_the_others(engine, db, synced):
    await _seed(db)

    result = await _operation().bulk_activate_openvpn_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 2 users"}
    stored = await _stored(engine)
    assert stored["entitled"]["openvpn"]["password"]
    assert stored["expired"]["openvpn"]["password"]
    assert stored["entitled"]["openvpn"]["password"] != stored["expired"]["openvpn"]["password"]
    assert stored["holder"]["openvpn"]["password"] == KEPT_OVPN
    for username in ("outsider", "switched_off", "no_groups"):
        assert stored[username]["openvpn"]["password"] is None
    assert synced == [["entitled", "expired"]]


@pytest.mark.asyncio
async def test_the_openvpn_activation_writes_only_the_openvpn_password(engine, db, synced):
    await _seed(db)
    before = await _stored(engine)

    await _operation().bulk_activate_openvpn_passwords(db, BulkUserFilter())

    after = await _stored(engine)
    for key in set(before["entitled"]) | set(after["entitled"]):
        if key != "openvpn":
            assert after["entitled"].get(key) == before["entitled"].get(key)
    assert after["entitled"]["mtproto"]["secret"] is None
    assert after["entitled"]["future_key"] == {"kept": True}


@pytest.mark.asyncio
async def test_the_filter_narrows_the_scope_but_never_widens_entitlement(engine, db, synced):
    ids = await _seed(db)

    result = await _operation().bulk_activate_openvpn_passwords(
        db, BulkUserFilter(users={ids["expired"], ids["outsider"]})
    )

    assert result == {"detail": "operation has been successfuly done on 1 users"}
    stored = await _stored(engine)
    assert stored["expired"]["openvpn"]["password"]
    assert stored["entitled"]["openvpn"]["password"] is None
    assert stored["outsider"]["openvpn"]["password"] is None


@pytest.mark.asyncio
async def test_a_second_run_changes_nothing(engine, db, synced):
    await _seed(db)
    await _operation().bulk_activate_openvpn_passwords(db, BulkUserFilter())
    first = await _stored(engine)

    result = await _operation().bulk_activate_openvpn_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 0 users"}
    assert await _stored(engine) == first
    assert len(synced) == 1


@pytest.mark.asyncio
async def test_an_unreadable_openvpn_section_is_left_alone_and_the_rest_still_run(engine, db, synced):
    await _seed(db)
    broken = (await db.execute(select(User).where(User.username == "entitled"))).scalar_one()
    broken.proxy_settings = {**broken.proxy_settings, "openvpn": {"password": ["not", "a", "string"]}}
    await db.commit()

    result = await _operation().bulk_activate_openvpn_passwords(db, BulkUserFilter())

    assert result == {"detail": "operation has been successfuly done on 1 users"}
    stored = await _stored(engine)
    assert stored["entitled"]["openvpn"]["password"] == ["not", "a", "string"]
    assert stored["expired"]["openvpn"]["password"]


@pytest.mark.asyncio
async def test_the_cli_operator_gets_the_count(engine, db, synced):
    await _seed(db)
    operation = _operation()
    operation.operator_type = OperatorType.CLI

    assert await operation.bulk_activate_openvpn_passwords(db, BulkUserFilter()) == 2


@pytest.mark.asyncio
async def test_the_mtproto_activation_keeps_its_contract(engine, db, synced):
    await _seed(db)

    dry = await _operation().bulk_activate_mtproto_secrets(db, BulkUserFilter(dry_run=True))
    result = await _operation().bulk_activate_mtproto_secrets(db, BulkUserFilter())

    assert dry.affected_users == 2
    assert result == {"detail": "operation has been successfuly done on 2 users"}
    stored = await _stored(engine)
    for username in ("entitled", "expired"):
        secret = stored[username]["mtproto"]["secret"]
        assert len(secret) == 32
        int(secret, 16)
    assert stored["holder"]["mtproto"]["secret"] == KEPT_MTP
    for username in ("outsider", "switched_off", "no_groups"):
        assert stored[username]["mtproto"]["secret"] is None
        assert stored[username]["openvpn"]["password"] is None


@pytest.mark.asyncio
async def test_the_mtproto_activation_never_hands_out_a_secret_another_user_already_holds(
    engine, db, synced, monkeypatch
):
    await _seed(db)
    values = iter([KEPT_MTP, KEPT_MTP, "f" * 32, "e" * 32])
    spec = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.mtproto_secret]
    monkeypatch.setitem(
        entitled_module.ENTITLED_SPEC_BY_FIELD,
        EntitledSecretField.mtproto_secret,
        replace(spec, generate=lambda: next(values)),
    )

    await _operation().bulk_activate_mtproto_secrets(db, BulkUserFilter())

    stored = await _stored(engine)
    issued = {stored[name]["mtproto"]["secret"] for name in ("entitled", "expired")}
    assert issued == {"f" * 32, "e" * 32}
    assert stored["holder"]["mtproto"]["secret"] == KEPT_MTP


@pytest.mark.asyncio
async def test_the_mtproto_activation_is_not_blocked_by_an_unrelated_broken_wireguard_key(engine, db, synced):
    await _seed(db)
    user = (await db.execute(select(User).where(User.username == "entitled"))).scalar_one()
    user.proxy_settings = {**user.proxy_settings, "wireguard": {"private_key": "not-a-real-key"}}
    await db.commit()

    await _operation().bulk_activate_mtproto_secrets(db, BulkUserFilter())

    stored = await _stored(engine)
    assert stored["entitled"]["mtproto"]["secret"]
    assert stored["entitled"]["wireguard"] == {"private_key": "not-a-real-key"}


@pytest.mark.asyncio
async def test_generation_fails_closed_when_it_cannot_find_a_free_value(engine, db, monkeypatch):
    await _seed(db)
    spec = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.openvpn_password]
    frozen = replace(spec, generate=lambda: KEPT_OVPN)

    with pytest.raises(ProxySecretUniquenessError) as raised:
        await issue_unique_secrets(db, frozen, 1)

    assert raised.value.fields == ["openvpn.password"]


@pytest.mark.asyncio
async def test_a_value_issued_earlier_in_the_same_batch_is_not_issued_twice(engine, db):
    await _seed(db)
    values = iter(["same", "same", "other"])
    spec = entitled_module.ENTITLED_SPEC_BY_FIELD[EntitledSecretField.openvpn_password]
    stepping = replace(spec, generate=lambda: next(values))
    reserved: dict = {}

    issued = await issue_unique_secrets(db, stepping, 2, reserved)

    assert issued == ["same", "other"]
    assert reserved[EntitledSecretField.openvpn_password] == {"same", "other"}


@pytest.mark.asyncio
async def test_the_driver_works_in_bounded_chunks_and_commits_each_one(engine, db):
    await _seed(db)
    extra_group = (await db.execute(select(Group).where(Group.name == "vpn"))).scalar_one()
    admin = (await db.execute(select(Admin))).scalar_one()
    for index in range(3):
        user = User(username=f"bulk{index}", proxy_settings=_settings(), admin_id=admin.id)
        user.groups = [extra_group]
        db.add(user)
    await db.commit()

    commits = []
    event.listen(engine.sync_engine, "commit", lambda conn: commits.append(1))
    chunks = []

    async def record(users):
        chunks.append(len(users))

    run = await driver.issue_missing_entitled_secrets(
        db,
        EntitledSecretField.openvpn_password,
        load_users=driver.load_users_for_sync,
        sync_users=record,
        chunk_size=2,
    )

    assert run.entitled == 6
    assert run.granted == 5
    assert sum(chunks) == 5
    assert max(chunks) <= 2
    assert len(commits) == len(chunks)
    assert not any(isinstance(obj, User) for obj in db.identity_map.values())


@pytest.mark.asyncio
async def test_no_core_of_the_protocol_means_nobody_is_touched(engine, db, synced):
    await _seed(db)
    before = await _stored(engine)
    for core in (await db.execute(select(CoreConfig))).scalars().all():
        await db.delete(core)
    await db.commit()

    result = await _operation().bulk_activate_openvpn_passwords(db, BulkUserFilter(dry_run=True))

    assert result.affected_users == 0
    assert await _stored(engine) == before


def test_the_openvpn_activation_route_uses_the_same_permission_and_body_as_the_other_activations():
    from app.fork.routers import user as router_module

    routes = {route.path: route for route in router_module.router.routes}
    openvpn = routes["/api/users/bulk/openvpn_activate"]
    l2tp = routes["/api/users/bulk/l2tp_activate"]

    def guard(route):
        (dependency,) = [dep.call for dep in route.dependant.dependencies if dep.call.__qualname__.endswith("_check")]
        return dependency.__qualname__, sorted(cell.cell_contents for cell in dependency.__closure__)

    assert openvpn.methods == {"POST"}
    assert guard(openvpn) == guard(l2tp) == ("require_scope_all.<locals>._check", ["update", "users"])
    assert openvpn.dependant.body_params[0].field_info.annotation is BulkUserFilter
    assert l2tp.dependant.body_params[0].field_info.annotation is BulkUserFilter
