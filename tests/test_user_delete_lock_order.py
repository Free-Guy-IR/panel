from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.crud import user as user_crud
from app.db.crud.wireguard import _lock_subnet_rows, get_wg_cores, wg_namespaces
from app.db.models import CoreConfig, User
from app.fork.proxy_secrets.locks import acquire_proxy_secrets_lock
from app.models.core import CoreType
from tests.test_recorder_harness import _database_url, recorder_db_fixture, seed_users_and_nodes  # noqa: F401

pytestmark = pytest.mark.skipif(
    _database_url().startswith("sqlite"), reason="needs a server database with row locks and deadlock detection"
)

WG_TAG = "wg-lock-order"


async def _seed_wireguard_user(recorder_db) -> int:
    _, user_ids, _ = await seed_users_and_nodes(recorder_db, user_count=1, node_count=0)
    async with recorder_db() as session:
        session.add(
            CoreConfig(
                name=WG_TAG,
                type=CoreType.wg,
                config={"interface_name": WG_TAG, "address": ["10.91.0.1/24"], "listen_port": 51820},
            )
        )
        await session.commit()
    return user_ids[0]


async def _hold_like_a_group_changing_modify(session, user_id: int) -> None:
    await acquire_proxy_secrets_lock(session)
    await session.execute(select(User.id).where(User.id == user_id).with_for_update())


@pytest.mark.asyncio
async def test_a_user_delete_overlapping_a_group_changing_modify_neither_deadlocks_nor_loses_either_side(recorder_db):
    user_id = await _seed_wireguard_user(recorder_db)
    events: list[str] = []

    async def delete_user() -> None:
        async with recorder_db() as delete_session:
            victim = (await delete_session.execute(select(User).where(User.id == user_id))).scalar_one()
            await user_crud.remove_users(delete_session, [victim])
            events.append("delete committed")

    async with recorder_db() as modify_session:
        await _hold_like_a_group_changing_modify(modify_session, user_id)
        delete_task = asyncio.create_task(delete_user())
        await asyncio.sleep(1.5)
        await _lock_subnet_rows(modify_session, wg_namespaces(await get_wg_cores(modify_session)))
        await modify_session.commit()
        events.append("modify committed")
    await asyncio.wait_for(delete_task, timeout=30)

    async with recorder_db() as session:
        remaining = (await session.execute(select(User.id).where(User.id == user_id))).scalars().all()
    assert events == ["modify committed", "delete committed"], events
    assert remaining == []


@pytest.mark.asyncio
async def test_an_expired_cleanup_overlapping_a_group_changing_modify_neither_deadlocks_nor_loses_either_side(
    recorder_db,
):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.db.models import UserStatus

    user_id = await _seed_wireguard_user(recorder_db)
    async with recorder_db() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(status=UserStatus.limited, last_status_change=datetime.now(UTC) - timedelta(days=40))
        )
        await session.commit()
    events: list[str] = []

    async def cleanup() -> None:
        async with recorder_db() as delete_session:
            deleted = await user_crud.remove_expired_users(delete_session, target="limited")
            events.append(f"cleanup committed {len(deleted)}")

    async with recorder_db() as modify_session:
        await _hold_like_a_group_changing_modify(modify_session, user_id)
        cleanup_task = asyncio.create_task(cleanup())
        await asyncio.sleep(1.5)
        await _lock_subnet_rows(modify_session, wg_namespaces(await get_wg_cores(modify_session)))
        await modify_session.commit()
        events.append("modify committed")
    await asyncio.wait_for(cleanup_task, timeout=30)

    assert events == ["modify committed", "cleanup committed 1"], events
