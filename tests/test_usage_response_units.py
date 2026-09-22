import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import System
from app.models.admin import AdminDetails, AdminRoleData
from app.operation.system import SystemOperation
from app.telegram.utils.shared import readable_size
from app.telegram.utils.texts import Message

SYSTEM_UPLINK = 1_000_000
SYSTEM_DOWNLINK = 3_000_000
SCOPED_ADMIN_USED_TRAFFIC = 7_777_777


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async with session_factory() as db:
            db.add(System(uplink=SYSTEM_UPLINK, downlink=SYSTEM_DOWNLINK))
            await db.flush()
            yield db
    finally:
        await engine.dispose()


def _owner():
    return AdminDetails(id=1, username="owner", role=AdminRoleData(is_owner=True))


def _scoped_admin():
    return AdminDetails(
        id=2,
        username="scoped",
        used_traffic=SCOPED_ADMIN_USED_TRAFFIC,
        role=AdminRoleData(is_owner=False),
    )


@pytest.mark.asyncio
async def test_owner_bandwidth_fields_carry_raw_directional_counters(session):
    stats = await SystemOperation.get_system_users_stats(session, admin=_owner())

    assert stats.incoming_bandwidth == SYSTEM_UPLINK
    assert stats.outgoing_bandwidth == SYSTEM_DOWNLINK
    assert stats.admin_used_traffic is None


@pytest.mark.asyncio
async def test_scoped_admin_used_traffic_is_not_reported_as_outgoing_bandwidth(session):
    stats = await SystemOperation.get_system_users_stats(session, admin=_scoped_admin())

    assert stats.outgoing_bandwidth != SCOPED_ADMIN_USED_TRAFFIC
    assert stats.incoming_bandwidth == 0
    assert stats.outgoing_bandwidth == 0


@pytest.mark.asyncio
async def test_scoped_admin_used_traffic_is_reported_in_its_own_field(session):
    stats = await SystemOperation.get_system_users_stats(session, admin=_scoped_admin())

    assert stats.admin_used_traffic == SCOPED_ADMIN_USED_TRAFFIC


@pytest.mark.asyncio
async def test_get_system_stats_propagates_admin_used_traffic(session):
    owner_stats = await SystemOperation.get_system_stats(session, admin=_owner())
    scoped_stats = await SystemOperation.get_system_stats(session, admin=_scoped_admin())

    assert owner_stats.admin_used_traffic is None
    assert owner_stats.incoming_bandwidth == SYSTEM_UPLINK
    assert owner_stats.outgoing_bandwidth == SYSTEM_DOWNLINK
    assert scoped_stats.admin_used_traffic == SCOPED_ADMIN_USED_TRAFFIC
    assert scoped_stats.outgoing_bandwidth == 0


@pytest.mark.asyncio
async def test_telegram_start_text_reports_the_scoped_admin_figure(session):
    scoped_stats = await SystemOperation.get_system_stats(session, admin=_scoped_admin())
    owner_stats = await SystemOperation.get_system_stats(session, admin=_owner())

    assert readable_size(SCOPED_ADMIN_USED_TRAFFIC) in Message.start(scoped_stats)
    assert readable_size(SYSTEM_UPLINK + SYSTEM_DOWNLINK) in Message.start(owner_stats)


@pytest.mark.asyncio
async def test_bandwidth_fields_carry_only_raw_system_counters_in_both_roles(session):
    owner_stats = await SystemOperation.get_system_users_stats(session, admin=_owner())
    scoped_stats = await SystemOperation.get_system_users_stats(session, admin=_scoped_admin())

    assert (owner_stats.admin_used_traffic is None) != (scoped_stats.admin_used_traffic is None)
    assert scoped_stats.incoming_bandwidth + scoped_stats.outgoing_bandwidth == 0
    assert owner_stats.incoming_bandwidth + owner_stats.outgoing_bandwidth == SYSTEM_UPLINK + SYSTEM_DOWNLINK
