"""Changing who is checked has to change who the review list shows.

A state row is only rewritten while its user is being checked, so a user who
falls out of scope keeps their row with nothing left to update it. The review
list would go on showing them - frozen at whatever their last check said - for
as long as the row survives.
"""

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Admin, Base, Group, User, UserConnectionLimit, UserConnectionState
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import prune_out_of_scope


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _seed(db) -> dict[str, int]:
    """Two groups, one user in each, and a third the owner disabled."""
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    await db.flush()

    blue, red = Group(name="blue", inbounds=[]), Group(name="red", inbounds=[])
    db.add_all([blue, red])
    await db.flush()

    ids = {}
    for name, groups, status in (("in_blue", [blue], "active"), ("in_red", [red], "active"), ("disabled", [blue], "disabled")):
        user = User(username=name, proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.groups = groups
        user.status = status
        db.add(user)
        await db.flush()
        db.add(UserConnectionState(user_id=user.id, devices=3, verdict="over_limit"))
        ids[name] = user.id

    await db.commit()
    ids["blue"], ids["red"] = blue.id, red.id
    return ids


async def _remaining(db) -> set[int]:
    return set((await db.execute(select(UserConnectionState.user_id))).scalars().all())


@pytest.mark.asyncio
async def test_narrowing_to_a_group_forgets_everyone_else(db):
    ids = await _seed(db)

    await prune_out_of_scope(db, ConnectionLimit(apply_to_group_ids=[ids["blue"]]))
    await db.commit()

    assert await _remaining(db) == {ids["in_blue"]}


@pytest.mark.asyncio
async def test_switching_groups_forgets_the_group_before(db):
    """The reported case: pick another group, and the old one has to clear."""
    ids = await _seed(db)

    await prune_out_of_scope(db, ConnectionLimit(apply_to_group_ids=[ids["blue"]]))
    await db.commit()
    assert await _remaining(db) == {ids["in_blue"]}

    # The scope moves to the other group, and the next cycle records the user
    # it now covers. What must not survive is the user it no longer does.
    await prune_out_of_scope(db, ConnectionLimit(apply_to_group_ids=[ids["red"]]))
    db.add(UserConnectionState(user_id=ids["in_red"], devices=3, verdict="over_limit"))
    await db.commit()

    assert await _remaining(db) == {ids["in_red"]}


@pytest.mark.asyncio
async def test_covering_every_group_keeps_the_active_users(db):
    ids = await _seed(db)

    await prune_out_of_scope(db, ConnectionLimit())
    await db.commit()

    # The disabled user goes either way: nothing will ever check them again.
    assert await _remaining(db) == {ids["in_blue"], ids["in_red"]}


@pytest.mark.asyncio
async def test_an_exempt_user_is_forgotten_rather_than_left_frozen(db):
    ids = await _seed(db)
    db.add(UserConnectionLimit(user_id=ids["in_blue"], exempt=True))
    await db.commit()

    await prune_out_of_scope(db, ConnectionLimit())
    await db.commit()

    assert await _remaining(db) == {ids["in_red"]}


@pytest.mark.asyncio
async def test_a_covered_user_who_is_simply_offline_is_kept(db):
    """Being offline is why a check is skipped, not a reason to forget them."""
    ids = await _seed(db)

    # online_at is left unset for every seeded user, so all of them are offline.
    await prune_out_of_scope(db, ConnectionLimit())
    await db.commit()

    assert ids["in_blue"] in await _remaining(db)
