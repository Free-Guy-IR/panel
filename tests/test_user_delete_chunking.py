import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.crud.user as crud_user
from app.db.crud.user import remove_expired_users, remove_users
from app.db.models import Admin, Base, Group, User, UserStatus
from app.models.proxy import ProxyTable


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _users(db, count: int, status: UserStatus = UserStatus.limited) -> list[User]:
    admin = Admin(username="owner", hashed_password="x", role_id=1)
    db.add(admin)
    await db.flush()
    group = Group(name="blue", inbounds=[])
    db.add(group)
    await db.flush()
    made = []
    for n in range(count):
        user = User(username=f"user{n}", proxy_settings=ProxyTable().dict(), admin_id=admin.id)
        user.groups = [group]
        user.status = status
        db.add(user)
        made.append(user)
    await db.commit()
    return made


def _count_commits(db, monkeypatch) -> list[int]:
    calls = [0]
    original = db.commit

    async def counting_commit(*args, **kwargs):
        calls[0] += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "commit", counting_commit)
    return calls


@pytest.mark.asyncio
async def test_the_cleanup_commits_each_chunk_instead_of_holding_one_transaction(db, monkeypatch):
    await _users(db, 5)
    monkeypatch.setattr(crud_user, "DELETE_CHUNK_SIZE", 2)
    commits = _count_commits(db, monkeypatch)

    deleted = await remove_expired_users(db, target="limited")

    assert sorted(deleted) == [f"user{n}" for n in range(5)]
    assert commits[0] == 3  # 2 + 2 + 1, not one transaction for all five
    assert (await db.scalar(select(func.count()).select_from(User))) == 0


@pytest.mark.asyncio
async def test_deleting_by_id_commits_each_chunk_too(db, monkeypatch):
    users = await _users(db, 5)
    monkeypatch.setattr(crud_user, "DELETE_CHUNK_SIZE", 2)
    commits = _count_commits(db, monkeypatch)

    await remove_users(db, users)

    assert commits[0] == 3
    assert (await db.scalar(select(func.count()).select_from(User))) == 0


@pytest.mark.asyncio
async def test_a_user_listed_twice_is_deleted_once(db, monkeypatch):
    users = await _users(db, 2)
    monkeypatch.setattr(crud_user, "DELETE_CHUNK_SIZE", 100)

    await remove_users(db, users + [users[0]])

    assert (await db.scalar(select(func.count()).select_from(User))) == 0


@pytest.mark.asyncio
async def test_what_comes_back_is_only_what_was_committed(db, monkeypatch):
    await _users(db, 4)
    monkeypatch.setattr(crud_user, "DELETE_CHUNK_SIZE", 2)

    original = db.commit
    state = {"n": 0}

    async def fail_on_second(*args, **kwargs):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("database went away")
        return await original(*args, **kwargs)

    monkeypatch.setattr(db, "commit", fail_on_second)

    with pytest.raises(RuntimeError):
        await remove_expired_users(db, target="limited")

    monkeypatch.setattr(db, "commit", original)
    assert (await db.scalar(select(func.count()).select_from(User))) == 2


@pytest.mark.asyncio
async def test_a_dry_run_deletes_nothing(db):
    await _users(db, 3)

    listed = await remove_expired_users(db, target="limited", dry_run=True)

    assert sorted(listed) == ["user0", "user1", "user2"]
    assert (await db.scalar(select(func.count()).select_from(User))) == 3
