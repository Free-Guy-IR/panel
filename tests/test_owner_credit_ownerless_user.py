from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from app.db.crud.user import bulk_set_owner, set_owner
from app.db.models import Admin, User
from app.models.proxy import ProxyTable
from tests.test_recorder_harness import recorder_db_fixture  # noqa: F401

ORPHAN_TRAFFIC = 700


async def _seed(session_factory) -> tuple[int, int]:
    tag = uuid4().hex[:10]
    async with session_factory() as session:
        admin = Admin(username=f"owner-credit-new-{tag}", hashed_password="secret", role_id=3)
        session.add(admin)
        await session.flush()
        user = User(username=f"owner-credit-orphan-{tag}", admin_id=None, proxy_settings=ProxyTable().dict(no_obj=True))
        user.used_traffic = ORPHAN_TRAFFIC
        session.add(user)
        await session.commit()
        return admin.id, user.id


async def _admin_traffic(session_factory, admin_id: int) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(Admin.used_traffic).where(Admin.id == admin_id)) or 0)


@pytest.mark.asyncio
async def test_bulk_set_owner_does_not_charge_an_ownerless_users_traffic_to_the_new_owner(recorder_db):
    admin_id, user_id = await _seed(recorder_db)
    async with recorder_db() as session:
        await bulk_set_owner(session, [await session.get(User, user_id)], await session.get(Admin, admin_id))
        await session.commit()

    assert await _admin_traffic(recorder_db, admin_id) == 0


@pytest.mark.asyncio
async def test_set_owner_does_not_charge_an_ownerless_users_traffic_to_the_new_owner(recorder_db):
    admin_id, user_id = await _seed(recorder_db)
    async with recorder_db() as session:
        await set_owner(session, await session.get(User, user_id), await session.get(Admin, admin_id))
        await session.commit()

    assert await _admin_traffic(recorder_db, admin_id) == 0
