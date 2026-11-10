from __future__ import annotations

from datetime import UTC, datetime as dt

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import base
from app.db.crud.user import get_users
from app.db.models import User, UserStatus
from app.models.user import UserListQuery, UserSortOption

SHARED_CREATED_AT = dt(2026, 1, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None)


@pytest.fixture
async def sort_db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def _seed_tied_users(session) -> list[int]:
    users = [User(username=f"tied{index}", status=UserStatus.active) for index in range(4)]
    session.add_all(users)
    await session.flush()
    for user in users:
        user.created_at = SHARED_CREATED_AT
    await session.commit()
    return [user.id for user in users]


def _captured_sql(session) -> list[str]:
    statements: list[str] = []
    engine = session.get_bind()

    @event.listens_for(engine, "before_cursor_execute")
    def _collect(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    session.info["_sort_sql_listener"] = _collect
    return statements


def _order_by(statements: list[str]) -> str:
    selects = [text for text in statements if " FROM users" in text and "ORDER BY" in text]
    assert selects, statements
    return selects[-1].split("ORDER BY", 1)[1].split(" LIMIT", 1)[0]


@pytest.mark.asyncio
async def test_ascending_user_sort_breaks_ties_on_id(sort_db):
    await _seed_tied_users(sort_db)
    statements = _captured_sql(sort_db)

    await get_users(sort_db, UserListQuery(sort=[UserSortOption.created_at], limit=2))

    assert _order_by(statements).strip().endswith("users.id ASC")


@pytest.mark.asyncio
async def test_descending_user_sort_breaks_ties_on_id_in_the_same_direction(sort_db):
    await _seed_tied_users(sort_db)
    statements = _captured_sql(sort_db)

    await get_users(sort_db, UserListQuery(sort=[UserSortOption.desc_created_at], limit=2))

    assert _order_by(statements).strip().endswith("users.id DESC")


@pytest.mark.asyncio
async def test_user_list_count_drops_the_page_ordering_and_the_subquery(sort_db):
    await _seed_tied_users(sort_db)
    statements = _captured_sql(sort_db)

    _, total = await get_users(
        sort_db, UserListQuery(sort=[UserSortOption.created_at], limit=2), return_with_count=True
    )

    counts = [text for text in statements if "count(" in text]
    assert len(counts) == 1, statements
    assert counts[0].startswith("SELECT count(users.id)")
    assert "ORDER BY" not in counts[0]
    assert "FROM (SELECT" not in counts[0]
    assert total == 4


@pytest.mark.asyncio
async def test_offset_pages_of_tied_users_never_repeat_or_skip_a_row(sort_db):
    expected = set(await _seed_tied_users(sort_db))

    first = await get_users(sort_db, UserListQuery(sort=[UserSortOption.created_at], limit=2, offset=0))
    second = await get_users(sort_db, UserListQuery(sort=[UserSortOption.created_at], limit=2, offset=2))

    first_ids = [user.id for user in first]
    second_ids = [user.id for user in second]
    assert first_ids == sorted(first_ids)
    assert second_ids == sorted(second_ids)
    assert not set(first_ids) & set(second_ids)
    assert set(first_ids) | set(second_ids) == expected
