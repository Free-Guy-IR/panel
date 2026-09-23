from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import base
from app.db.crud.user import autodelete_expired_users, get_user_by_id, reset_user_data_usage
from app.db.models import Admin, AdminRole, User, UserStatus
from app.models.proxy import ProxyTable


@pytest.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a4.db'}", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(base.Base.metadata.create_all)
    try:
        yield async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


async def _seed(factory, **users) -> dict[str, int]:
    async with factory() as session:
        role = AdminRole(name="r", is_owner=False, permissions={}, limits={}, features={}, access={})
        session.add(role)
        await session.flush()
        admin = Admin(username="a", hashed_password="x", role_id=role.id)
        session.add(admin)
        await session.flush()
        made = {}
        for name, fields in users.items():
            user = User(username=name, admin_id=admin.id, proxy_settings=ProxyTable().dict(no_obj=True))
            for key, value in fields.items():
                setattr(user, key, value)
            session.add(user)
            made[name] = user
        await session.commit()
        return {name: user.id for name, user in made.items()}


async def test_a_usage_reset_does_not_undo_a_disable_committed_after_the_user_was_loaded(sessions):
    ids = await _seed(sessions, heavy={"status": UserStatus.limited, "data_limit": 10, "used_traffic": 10})
    async with sessions() as request_session:
        db_user = await get_user_by_id(request_session, ids["heavy"])
        async with sessions() as other_admin:
            await other_admin.execute(update(User).where(User.id == ids["heavy"]).values(status=UserStatus.disabled))
            await other_admin.commit()
        await reset_user_data_usage(request_session, db_user)
    async with sessions() as check:
        final = (await check.execute(select(User.status).where(User.id == ids["heavy"]))).scalar_one()
    assert final == UserStatus.disabled, f"the concurrent disable was overwritten; final status {final}"


async def test_one_user_with_a_huge_auto_delete_value_does_not_stop_auto_delete_for_everyone(sessions):
    long_ago = datetime.now(UTC) - timedelta(days=400)
    ids = await _seed(
        sessions,
        due={"status": UserStatus.expired, "last_status_change": long_ago, "auto_delete_in_days": 0},
        never={"status": UserStatus.expired, "last_status_change": long_ago, "auto_delete_in_days": 99_999_999},
    )
    async with sessions() as session:
        try:
            await autodelete_expired_users(session)
        except OverflowError as exc:
            pytest.fail(f"auto-delete job crashed for every user: {exc!r}")
    async with sessions() as check:
        remaining = set((await check.execute(select(User.id))).scalars().all())
    assert ids["due"] not in remaining


async def test_a_bulk_data_limit_cut_reports_every_user_whose_status_it_changed(sessions):
    from sqlalchemy.sql.dml import Update

    from app.db.crud.bulk import update_users_datalimit
    from app.models.user import BulkUser

    gb = 1024**3
    ids = await _seed(sessions, target={"status": UserStatus.disabled, "data_limit": 10 * gb, "used_traffic": 7 * gb})
    async with sessions() as request_session:
        original_execute = request_session.execute

        async def execute_with_concurrent_enable(statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == "users":
                async with sessions() as other_admin:
                    await other_admin.execute(
                        update(User).where(User.id == ids["target"]).values(status=UserStatus.active)
                    )
                    await other_admin.commit()
            return await original_execute(statement, *args, **kwargs)

        request_session.execute = execute_with_concurrent_enable
        changed, _ = await update_users_datalimit(request_session, BulkUser(amount=-5 * gb, users=[ids["target"]]))
    async with sessions() as check:
        final = (await check.execute(select(User.status).where(User.id == ids["target"]))).scalar_one()
    reported = {user.id for user in changed}
    assert final == UserStatus.active or ids["target"] in reported, (
        f"UPDATE turned the user {final} but it is missing from the users handed to sync_users ({reported}); "
        f"the node keeps the active user pushed by the concurrent enable"
    )


async def test_a_modify_does_not_undo_a_disable_committed_after_the_user_was_loaded(sessions):
    from app.db.crud.user import modify_user
    from app.models.user import UserModify

    ids = await _seed(sessions, target={"status": UserStatus.limited, "data_limit": 10, "used_traffic": 10})
    async with sessions() as request_session:
        db_user = await get_user_by_id(request_session, ids["target"])
        async with sessions() as other_admin:
            await other_admin.execute(update(User).where(User.id == ids["target"]).values(status=UserStatus.disabled))
            await other_admin.commit()
        await modify_user(request_session, db_user, UserModify(data_limit=100))
    async with sessions() as check:
        final = (await check.execute(select(User.status, User.data_limit).where(User.id == ids["target"]))).one()
    assert (final.status, final.data_limit) == (UserStatus.disabled, 100), final


async def test_a_modify_decides_from_the_usage_billed_after_the_user_was_loaded(sessions):
    from app.db.crud.user import modify_user
    from app.models.user import UserModify

    ids = await _seed(sessions, target={"status": UserStatus.active, "data_limit": 100, "used_traffic": 10})
    async with sessions() as request_session:
        db_user = await get_user_by_id(request_session, ids["target"])
        async with sessions() as recorder:
            await recorder.execute(update(User).where(User.id == ids["target"]).values(used_traffic=90))
            await recorder.commit()
        await modify_user(request_session, db_user, UserModify(data_limit=50))
    async with sessions() as check:
        final = (await check.execute(select(User.status).where(User.id == ids["target"]))).scalar_one()
    assert final == UserStatus.limited


async def test_a_bulk_reset_does_not_reenable_a_user_disabled_after_the_load(sessions):
    from app.db.crud.user import bulk_reset_user_data_usage

    ids = await _seed(
        sessions,
        blocked={"status": UserStatus.limited, "data_limit": 10, "used_traffic": 10},
        paying={"status": UserStatus.limited, "data_limit": 10, "used_traffic": 10},
    )
    async with sessions() as request_session:
        users = [
            await get_user_by_id(request_session, ids["blocked"]),
            await get_user_by_id(request_session, ids["paying"]),
        ]
        async with sessions() as other_admin:
            await other_admin.execute(update(User).where(User.id == ids["blocked"]).values(status=UserStatus.disabled))
            await other_admin.commit()
        returned = await bulk_reset_user_data_usage(request_session, users)
        returned_statuses = {user.id: user.status for user in returned}
    async with sessions() as check:
        final = dict((await check.execute(select(User.id, User.status).where(User.id.in_(ids.values())))).all())
    assert final == {ids["blocked"]: UserStatus.disabled, ids["paying"]: UserStatus.active}
    assert returned_statuses == final


async def test_a_bulk_expire_cut_reports_every_user_whose_status_it_changed(sessions):
    from sqlalchemy.sql.dml import Update

    from app.db.crud.bulk import update_users_expire
    from app.models.user import BulkUser

    ids = await _seed(
        sessions,
        target={"status": UserStatus.disabled, "expire": datetime.now(UTC) + timedelta(hours=1)},
    )
    async with sessions() as request_session:
        original_execute = request_session.execute

        async def execute_with_concurrent_enable(statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == "users":
                async with sessions() as other_admin:
                    await other_admin.execute(
                        update(User).where(User.id == ids["target"]).values(status=UserStatus.active)
                    )
                    await other_admin.commit()
            return await original_execute(statement, *args, **kwargs)

        request_session.execute = execute_with_concurrent_enable
        changed, count = await update_users_expire(request_session, BulkUser(amount=-7200, users=[ids["target"]]))
    async with sessions() as check:
        final = (await check.execute(select(User.status).where(User.id == ids["target"]))).scalar_one()
    assert count == 1
    assert final == UserStatus.active or ids["target"] in {user.id for user in changed}, final


async def test_a_negative_bulk_data_limit_reports_only_the_users_it_changed(sessions):
    from app.db.crud.bulk import update_users_datalimit
    from app.models.user import BulkUser

    gb = 1024**3
    ids = await _seed(
        sessions,
        big={"status": UserStatus.active, "data_limit": 10 * gb, "used_traffic": 1 * gb},
        small={"status": UserStatus.active, "data_limit": 3 * gb, "used_traffic": 1 * gb},
    )
    async with sessions() as session:
        changed, count = await update_users_datalimit(session, BulkUser(amount=-5 * gb, users=list(ids.values())))
    async with sessions() as check:
        final = dict((await check.execute(select(User.id, User.data_limit).where(User.id.in_(ids.values())))).all())
    assert count == 1 and changed == []
    assert final == {ids["big"]: 5 * gb, ids["small"]: 3 * gb}


async def test_an_empty_next_plan_is_dropped_instead_of_granting_an_unlimited_user(sessions):
    from app.db.crud.user import reset_user_by_next
    from app.db.models import NextPlan

    ids = await _seed(sessions, target={"status": UserStatus.limited, "data_limit": 10, "used_traffic": 10})
    async with sessions() as session:
        session.add(NextPlan(user_id=ids["target"], user_template_id=None, data_limit=0, expire=None))
        await session.commit()
    async with sessions() as session:
        db_user = await get_user_by_id(session, ids["target"])
        await reset_user_by_next(session, db_user)
    async with sessions() as check:
        final = (
            await check.execute(select(User.status, User.data_limit, User.used_traffic).where(User.id == ids["target"]))
        ).one()
        plans = (await check.execute(select(NextPlan.id).where(NextPlan.user_id == ids["target"]))).all()
    assert (final.status, final.data_limit, final.used_traffic) == (UserStatus.limited, 10, 10)
    assert plans == []


async def test_next_plan_applies_from_the_committed_usage_and_plan(sessions):
    from app.db.crud.user import reset_user_by_next
    from app.db.models import NextPlan

    ids = await _seed(sessions, target={"status": UserStatus.limited, "data_limit": 1000, "used_traffic": 400})
    async with sessions() as session:
        session.add(
            NextPlan(
                user_id=ids["target"], user_template_id=None, data_limit=5000, expire=None, add_remaining_traffic=True
            )
        )
        await session.commit()
    async with sessions() as request_session:
        db_user = await get_user_by_id(request_session, ids["target"])
        async with sessions() as recorder:
            await recorder.execute(update(User).where(User.id == ids["target"]).values(used_traffic=700))
            await recorder.commit()
        await reset_user_by_next(request_session, db_user)
    async with sessions() as check:
        final = (await check.execute(select(User.status, User.data_limit).where(User.id == ids["target"]))).one()
    assert (final.status, final.data_limit) == (UserStatus.active, 5300)


async def test_next_plan_removed_after_the_load_is_not_applied(sessions):
    from sqlalchemy import delete

    from app.db.crud.user import reset_user_by_next
    from app.db.models import NextPlan

    ids = await _seed(sessions, target={"status": UserStatus.limited, "data_limit": 1000, "used_traffic": 1000})
    async with sessions() as session:
        session.add(NextPlan(user_id=ids["target"], user_template_id=None, data_limit=0, expire=86400))
        await session.commit()
    async with sessions() as request_session:
        db_user = await get_user_by_id(request_session, ids["target"])
        async with sessions() as other_admin:
            await other_admin.execute(delete(NextPlan).where(NextPlan.user_id == ids["target"]))
            await other_admin.commit()
        await reset_user_by_next(request_session, db_user)
    async with sessions() as check:
        final = (
            await check.execute(select(User.status, User.data_limit, User.used_traffic).where(User.id == ids["target"]))
        ).one()
    assert (final.status, final.data_limit, final.used_traffic) == (UserStatus.limited, 1000, 1000)


async def test_cleanup_does_not_delete_a_user_renewed_after_it_was_listed(sessions):
    from app.db.crud.user import remove_expired_users

    long_ago = datetime.now(UTC) - timedelta(days=40)
    ids = await _seed(
        sessions,
        stale={"status": UserStatus.limited, "last_status_change": long_ago},
        renewed={"status": UserStatus.limited, "last_status_change": long_ago},
    )
    pushed: list[int] = []

    async def record_push(rows):
        pushed.extend(row.id for row in rows)

    async with sessions() as db:
        original_execute = db.execute
        calls = {"n": 0}

        async def execute_with_renewal_after_listing(statement, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                async with sessions() as admin_session:
                    await admin_session.execute(
                        update(User)
                        .where(User.id == ids["renewed"])
                        .values(status=UserStatus.active, last_status_change=datetime.now(UTC))
                    )
                    await admin_session.commit()
            return await original_execute(statement, *args, **kwargs)

        db.execute = execute_with_renewal_after_listing
        deleted = await remove_expired_users(db, target="limited", on_chunk_deleted=record_push)
    async with sessions() as check:
        remaining = set((await check.execute(select(User.id))).scalars().all())
    assert remaining == {ids["renewed"]}, (deleted, remaining)
    assert deleted == ["stale"]
    assert pushed == [ids["stale"]]
