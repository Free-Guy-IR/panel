from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select, update

from app import notification
from app.db.models import User, UserStatus
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.models.settings import ConnectionLimit
from app.models.user import BulkUsersSelection
from app.node import node_manager
from app.operation import OperatorType
from app.operation.user import UserOperation
from tests.a1_bridge_harness import RecordingNode, TestGetDB, isolate_node_manager, seed_group, seed_user, settle
from tests.api import TestSession
from tests.api.helpers import unique_name

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)
SETTINGS = ConnectionLimit(
    enabled=True,
    monitor_only=False,
    enforcement_enabled=True,
    persistence_cycles=1,
    punishment_steps=[30],
)


def _over_limit(user_id: int):
    return SimpleNamespace(
        user_id=user_id,
        verdict="over_limit",
        streak=5,
        devices=4,
        limit_applied=2,
        details={},
        reasons=["adv-r1a"],
    )


async def _user_on_a_node(monkeypatch) -> tuple[int, RecordingNode]:
    group_id = await seed_group(unique_name("adv_tag"))
    user_id = await seed_user(group_id, status=UserStatus.active)
    node = RecordingNode(2101)
    await node.connect("0.6.4", "26.3.27")
    isolate_node_manager(monkeypatch, node_manager, {2101: node})
    monkeypatch.setattr(connection_limiter, "GetDB", TestGetDB)
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("user_status_change", "modify_user"):
        monkeypatch.setattr(notification, name, AsyncMock())
    return user_id, node


async def _limiter_disables(user_id: int) -> None:
    async with TestGetDB() as limiter_db:
        await connection_limiter._enforce(limiter_db, [_over_limit(user_id)], SETTINGS)


async def _stored(user_id: int) -> tuple[UserStatus, list[ConnectionRestriction]]:
    async with TestSession() as session:
        status = (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()
        active = (
            (
                await session.execute(
                    select(ConnectionRestriction).where(
                        ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
    return status, list(active)


async def test_a_bulk_enable_that_loaded_the_user_before_a_limiter_disable_leaves_it_disabled_forever(monkeypatch):
    user_id, _node = await _user_on_a_node(monkeypatch)
    loader = UserOperation._get_validated_users_by_ids

    async def load_then_limiter_acts(self, db, ids, admin, **kwargs):
        users = await loader(self, db, ids, admin, **kwargs)
        await _limiter_disables(user_id)
        return users

    monkeypatch.setattr(UserOperation, "_get_validated_users_by_ids", load_then_limiter_acts)
    async with TestSession() as db:
        await UserOperation(OperatorType.API).bulk_enable_users(db, BulkUsersSelection(ids={user_id}), OWNER)
    await settle()

    status, active = await _stored(user_id)
    async with TestGetDB() as later:
        await later.execute(
            update(ConnectionRestriction)
            .where(ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(True))
            .values(restore_at=dt.now(UTC) - td(seconds=1))
        )
        await later.commit()
        await connection_limiter._release_due(later, SETTINGS)
    status_after_release, _ = await _stored(user_id)

    assert status_after_release != UserStatus.disabled, (
        f"the admin bulk-enabled user {user_id} while the device limiter disabled it for 30 minutes; the bulk "
        f"call closed the limiter's restriction but decided from its stale copy that the user was not disabled, "
        f"so the database holds {status.value} with {len(active)} active restriction(s) and nothing will ever "
        f"lift it (status after the limiter's release pass: {status_after_release.value})"
    )


async def test_a_single_enable_in_the_same_race_does_enable(monkeypatch):
    from app.db.crud.user import get_user_by_id
    from app.models.user import UserStatusToggle

    user_id, _node = await _user_on_a_node(monkeypatch)
    async with TestSession() as db:
        db_user = await get_user_by_id(db, user_id)
        await _limiter_disables(user_id)
        await UserOperation(OperatorType.API)._set_user_disabled(db, db_user, UserStatusToggle(disabled=False), OWNER)
    status, active = await _stored(user_id)
    assert (status, active) == (UserStatus.active, [])


async def _restricted_and_released_after_load(monkeypatch, user_id: int, operation_name: str) -> None:
    loader = UserOperation._get_validated_users_by_ids

    async def load_then_limiter_releases(self, db, ids, admin, **kwargs):
        users = await loader(self, db, ids, admin, **kwargs)
        async with TestGetDB() as later:
            await later.execute(
                update(ConnectionRestriction)
                .where(ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(True))
                .values(restore_at=dt.now(UTC) - td(seconds=1))
            )
            await later.commit()
            await connection_limiter._release_due(later, SETTINGS)
        return users

    monkeypatch.setattr(UserOperation, "_get_validated_users_by_ids", load_then_limiter_releases)
    async with TestSession() as db:
        await getattr(UserOperation(OperatorType.API), operation_name)(db, BulkUsersSelection(ids={user_id}), OWNER)
    await settle()


async def test_a_bulk_disable_that_loaded_the_user_before_the_limiter_released_it_still_disables_it(monkeypatch):
    user_id, node = await _user_on_a_node(monkeypatch)
    await _limiter_disables(user_id)
    assert (await _stored(user_id))[0] == UserStatus.disabled

    await _restricted_and_released_after_load(monkeypatch, user_id, "bulk_disable_users")

    status, active = await _stored(user_id)
    assert (status, active) == (UserStatus.disabled, []), (status, active)
    assert str(user_id) not in node.live, f"user {user_id} is disabled in the database but still on the node"


async def test_a_bulk_disable_of_a_user_the_limiter_disabled_after_the_load_outlives_the_restriction(monkeypatch):
    user_id, node = await _user_on_a_node(monkeypatch)
    loader = UserOperation._get_validated_users_by_ids

    async def load_then_limiter_acts(self, db, ids, admin, **kwargs):
        users = await loader(self, db, ids, admin, **kwargs)
        await _limiter_disables(user_id)
        return users

    monkeypatch.setattr(UserOperation, "_get_validated_users_by_ids", load_then_limiter_acts)
    async with TestSession() as db:
        await UserOperation(OperatorType.API).bulk_disable_users(db, BulkUsersSelection(ids={user_id}), OWNER)
    await settle()

    status, active = await _stored(user_id)
    async with TestGetDB() as later:
        await connection_limiter._release_due(later, SETTINGS)
    await settle()
    status_after_release, _ = await _stored(user_id)
    assert (status, active, status_after_release) == (UserStatus.disabled, [], UserStatus.disabled)
    assert str(user_id) not in node.live


async def test_a_bulk_enable_of_a_user_the_limiter_already_released_changes_nothing(monkeypatch):
    user_id, node = await _user_on_a_node(monkeypatch)
    await _limiter_disables(user_id)

    await _restricted_and_released_after_load(monkeypatch, user_id, "bulk_enable_users")

    status, active = await _stored(user_id)
    assert (status, active) == (UserStatus.active, [])
    assert node.live.get(str(user_id)), f"user {user_id} is active in the database but missing from the node"
