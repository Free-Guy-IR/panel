import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, ConnectionRestriction, Group, User, UserStatus
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.utils.connection_enforcement import restrict

REASONS = [
    {"code": "allowance", "count": 2},
    {"code": "networks", "count": 3, "items": ["2.147.8.0/24", "5.215.130.0/24"]},
    {"code": "hwid_by_model", "count": 3, "items": ["SM-A125F", "iPhone13,2"]},
]


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


class _Observation:
    def __init__(self, user_id, reasons=REASONS):
        self.user_id = user_id
        self.streak = 3
        self.verdict = "over_limit"
        self.devices = 3
        self.limit_applied = 2
        self.details = {"real_groups": ["2.147.8.0/24", "5.215.130.0/24"]}
        self.reasons = reasons


async def _user(db) -> User:
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    await db.flush()
    group = Group(name="blue", inbounds=[])
    db.add(group)
    await db.flush()
    user = User(username="someone", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
    user.groups = [group]
    user.status = UserStatus.active
    db.add(user)
    await db.commit()
    return user


@pytest.mark.asyncio
async def test_a_disable_records_what_it_rested_on(db):
    user = await _user(db)

    await restrict(db, user, _Observation(user.id), ConnectionLimit(punishment_steps=[-1]))
    await db.commit()

    row = (await db.execute(select(ConnectionRestriction))).scalar()
    assert [reason["code"] for reason in row.reasons] == ["allowance", "networks", "hwid_by_model"]
    assert row.reasons[2]["items"] == ["SM-A125F", "iPhone13,2"]
    assert row.ip_count == 3 and row.ip_limit == 2


@pytest.mark.asyncio
async def test_a_warning_records_them_too(db):
    user = await _user(db)

    assert await restrict(db, user, _Observation(user.id), ConnectionLimit(punishment_steps=[0, 10])) is None
    await db.commit()

    row = (await db.execute(select(ConnectionRestriction))).scalar()
    assert [reason["code"] for reason in row.reasons] == ["allowance", "networks", "hwid_by_model"]


@pytest.mark.asyncio
async def test_an_observation_with_nothing_to_say_stores_an_empty_list(db):
    user = await _user(db)

    await restrict(db, user, _Observation(user.id, reasons=None), ConnectionLimit(punishment_steps=[-1]))
    await db.commit()

    row = (await db.execute(select(ConnectionRestriction))).scalar()
    assert row.reasons == []
