from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.db.crud import user as user_crud
from app.db.models import User, UserStatus
from tests.test_recorder_harness import recorder_db_fixture, seed_users_and_nodes  # noqa: F401


@pytest.mark.asyncio
async def test_autodelete_does_not_delete_a_user_renewed_after_its_read(monkeypatch: pytest.MonkeyPatch, recorder_db):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=2, node_count=0)
    long_ago = datetime.now(UTC) - timedelta(days=40)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id.in_(user_ids))
            .values(
                status=UserStatus.expired,
                expire=long_ago,
                last_status_change=long_ago,
                auto_delete_in_days=30,
            )
        )
        await session.commit()
    renewed_id = user_ids[1]

    real_load = user_crud.load_user_attrs
    calls = {"n": 0}

    async def load_then_admin_renews_second_user(user, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            async with recorder_db() as admin_session:
                await admin_session.execute(
                    update(User)
                    .where(User.id == renewed_id)
                    .values(
                        status=UserStatus.active,
                        expire=datetime.now(UTC) + timedelta(days=30),
                        last_status_change=datetime.now(UTC),
                    )
                )
                await admin_session.commit()
        return await real_load(user, **kwargs)

    monkeypatch.setattr(user_crud, "load_user_attrs", load_then_admin_renews_second_user)

    async with recorder_db() as db:
        deleted = await user_crud.autodelete_expired_users(db)

    async with recorder_db() as session:
        remaining = (await session.execute(select(User.id, User.status).where(User.id.in_(user_ids)))).all()
    assert [row.id for row in remaining] == [renewed_id], (remaining, [user.id for user in deleted])


@pytest.mark.asyncio
async def test_autodelete_job_pushes_a_node_removal_for_exactly_the_deleted_users(
    monkeypatch: pytest.MonkeyPatch, recorder_db
):
    from app.jobs import record_usages, remove_expired_users as job

    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=3, node_count=0)
    long_ago = datetime.now(UTC) - timedelta(days=40)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id.in_(user_ids[:2]))
            .values(status=UserStatus.expired, last_status_change=long_ago, auto_delete_in_days=30)
        )
        await session.execute(
            update(User)
            .where(User.id == user_ids[2])
            .values(status=UserStatus.expired, last_status_change=datetime.now(UTC), auto_delete_in_days=30)
        )
        await session.commit()

    pushed: list[tuple[int, int]] = []

    async def record_removal(rows):
        pushed.extend((row.id, len(((row.proxy_settings or {}).get("vless") or {}).get("id") or "")) for row in rows)

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(job, "GetDB", record_usages.GetDB)
    monkeypatch.setattr(job, "sync_remove_users", record_removal)
    monkeypatch.setattr(job.notification, "remove_user", quiet)

    await job.remove_expired_users()

    async with recorder_db() as session:
        remaining = (await session.execute(select(User.id).where(User.id.in_(user_ids)))).scalars().all()
    assert list(remaining) == [user_ids[2]]
    assert sorted(user_id for user_id, _ in pushed) == sorted(user_ids[:2])
    assert all(id_length > 0 for _, id_length in pushed)


@pytest.mark.asyncio
async def test_autodelete_skips_rows_without_a_status_change_time(recorder_db):
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_ids[0])
            .values(status=UserStatus.expired, last_status_change=None, auto_delete_in_days=0)
        )
        await session.commit()

    async with recorder_db() as db:
        deleted = await user_crud.autodelete_expired_users(db)

    assert deleted == []
