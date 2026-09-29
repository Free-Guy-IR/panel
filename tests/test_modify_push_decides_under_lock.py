import asyncio
import random
from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app import notification
from app.db.crud.user import get_user_by_id
from app.db.crud.wireguard import _accessible_tags_stmt
from app.db.models import Group, User, UserStatus
from app.fork.jobs import connection_limiter, status_review
from app.jobs import review_users
from app.models.admin import AdminDetails, AdminRoleData
from app.models.group import GroupModify
from app.models.settings import ConnectionLimit, General
from app.models.user import BulkUsersSelection, UserModify
from app.node import bridge as node_bridge, node_manager, sync as node_sync
from app.node.bridge import _QueuedBatchSync
from app.operation import OperatorType
from app.operation.group import GroupOperation
from app.operation.user import UserOperation
from tests.a1_bridge_harness import RecordingNode, TestGetDB, isolate_node_manager, seed_group, seed_user, settle
from tests.api import TestSession
from tests.api.helpers import unique_name

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
LIMITS = ConnectionLimit(
    enabled=True,
    monitor_only=False,
    enforcement_enabled=True,
    persistence_cycles=1,
    punishment_steps=[30],
)
SERVING = (UserStatus.active, UserStatus.on_hold)


class QueuedRecordingNode(_QueuedBatchSync, RecordingNode):
    pass


class ReaderBehindWriters(node_sync.CurrentStateReader):
    def __init__(self, writers: asyncio.Lock):
        super().__init__()
        self._writers = writers

    async def _query(self, user_ids):
        async with self._writers:
            return await super()._query(user_ids)


def quiet(monkeypatch, writers: asyncio.Lock | None = None):
    for module in (review_users, status_review, node_sync, connection_limiter):
        monkeypatch.setattr(module, "GetDB", TestGetDB)
    monkeypatch.setattr(review_users, "general_settings", AsyncMock(return_value=General()))
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("user_status_change", "modify_user", "modify_group", "remove_user"):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(node_bridge, "_refresh_handler", node_sync.refresh_node_users)
    if writers is not None:
        monkeypatch.setattr(node_sync, "_current_state", ReaderBehindWriters(writers))


async def node_with(monkeypatch, node_id: int, users: dict[int, str]) -> QueuedRecordingNode:
    node = QueuedRecordingNode(node_id)
    await node.connect("0.6.4", "26.3.27")
    node.live.update({str(user_id): [tag] for user_id, tag in users.items()})
    isolate_node_manager(monkeypatch, node_manager, {node_id: node})
    return node


async def drained(node, timeout=15.0):
    async with asyncio.timeout(timeout):
        while True:
            await asyncio.sleep(0.2)
            if not await node._user_sync_store.has_pending(node.node_id):
                await asyncio.sleep(0.3)
                if not await node._user_sync_store.has_pending(node.node_id):
                    return


async def stored_status(user_id: int) -> UserStatus:
    async with TestSession() as session:
        return (await session.execute(select(User.status).where(User.id == user_id))).scalar_one()


def pushes_for(node, user_id: int):
    return [delivery for delivery in node.deliveries if delivery[1] == str(user_id)]


async def over_limit(user_id: int) -> None:
    observation = SimpleNamespace(
        user_id=user_id,
        verdict="over_limit",
        streak=5,
        devices=4,
        limit_applied=2,
        details={},
        reasons=["modify-under-lock"],
    )
    async with TestGetDB() as db:
        await connection_limiter._enforce(db, [observation], LIMITS)


async def test_a_renewal_loaded_before_the_expire_job_is_pushed_to_the_nodes(monkeypatch):
    quiet(monkeypatch)
    tag = unique_name("renew_tag")
    user_id = await seed_user(await seed_group(tag), expire=dt.now(UTC) - td(minutes=1))
    node = await node_with(monkeypatch, 2701, {user_id: tag})

    async with TestSession() as db:
        loaded = await get_user_by_id(db, user_id)
        await review_users.expire_users_job()
        assert await settle(lambda: str(user_id) not in node.live)
        await UserOperation(OperatorType.API)._modify_user(
            db, loaded, UserModify(status=UserStatus.active, expire=dt.now(UTC) + td(days=30)), OWNER
        )
    await drained(node)

    assert await stored_status(user_id) == UserStatus.active
    assert node.live.get(str(user_id)) == [tag], (
        f"the admin renewed user {user_id} from a copy loaded before the expire job removed it from the node; "
        f"the database holds active but the node holds {node.live.get(str(user_id))} "
        f"(pushes: {pushes_for(node, user_id)})"
    )


async def test_an_activation_loaded_before_the_limiter_disabled_the_user_is_pushed_to_the_nodes(monkeypatch):
    quiet(monkeypatch)
    tag = unique_name("limiter_tag")
    user_id = await seed_user(await seed_group(tag))
    node = await node_with(monkeypatch, 2702, {user_id: tag})

    async with TestSession() as db:
        loaded = await get_user_by_id(db, user_id)
        await over_limit(user_id)
        assert await settle(lambda: str(user_id) not in node.live)
        await UserOperation(OperatorType.API)._modify_user(db, loaded, UserModify(status=UserStatus.active), OWNER)
    await drained(node)

    assert await stored_status(user_id) == UserStatus.active
    assert node.live.get(str(user_id)) == [tag], (
        f"the admin activated user {user_id} from a copy loaded before the device limiter disabled it; the "
        f"database holds active but the node holds {node.live.get(str(user_id))} "
        f"(pushes: {pushes_for(node, user_id)})"
    )


async def test_a_save_of_the_loaded_groups_after_another_admin_moved_the_user_leaves_node_and_database_agreeing(
    monkeypatch,
):
    quiet(monkeypatch)
    first_tag, second_tag = unique_name("first_tag"), unique_name("second_tag")
    first_group, second_group = await seed_group(first_tag), await seed_group(second_tag)
    user_id = await seed_user(first_group)
    node = await node_with(monkeypatch, 2703, {user_id: first_tag})
    operator = UserOperation(OperatorType.API)

    async with TestSession() as db:
        loaded = await get_user_by_id(db, user_id)
        async with TestSession() as other:
            await operator.modify_user_by_id(other, user_id, UserModify(group_ids=[second_group]), OWNER)
        await drained(node)
        assert node.live.get(str(user_id)) == [second_tag]
        await operator._modify_user(db, loaded, UserModify(group_ids=[first_group], note="saved form"), OWNER)
    await drained(node)

    async with TestSession() as session:
        committed = sorted(tag for _, tag in (await session.execute(_accessible_tags_stmt(user_ids=[user_id]))).all())
    assert node.live.get(str(user_id)) == committed, (
        f"after two admins changed user {user_id}'s groups, the node holds {node.live.get(str(user_id))} but the "
        f"database grants {committed} (pushes: {pushes_for(node, user_id)})"
    )


async def test_a_disable_loaded_while_the_group_was_off_is_pushed_after_the_group_comes_back(monkeypatch):
    quiet(monkeypatch)
    tag = unique_name("masked_tag")
    group_id = await seed_group(tag)
    async with TestSession() as session:
        group = await session.get(Group, group_id)
        group.is_disabled = True
        group_name = group.name
        await session.commit()
    user_id = await seed_user(group_id)
    node = await node_with(monkeypatch, 2704, {})

    async with TestSession() as db:
        loaded = await get_user_by_id(db, user_id)
        async with TestSession() as other:
            await GroupOperation(OperatorType.API).modify_group(
                other, group_id, GroupModify(name=group_name, inbound_tags=None, is_disabled=False), OWNER
            )
        await drained(node)
        assert node.live.get(str(user_id)) == [tag]
        await UserOperation(OperatorType.API)._modify_user(db, loaded, UserModify(status=UserStatus.disabled), OWNER)
    await drained(node)

    assert await stored_status(user_id) == UserStatus.disabled
    assert str(user_id) not in node.live, (
        f"an admin disabled user {user_id} from a copy loaded while its only group was disabled; another admin "
        f"re-enabled the group in between, so the node serves the user although the database holds disabled "
        f"(pushes: {pushes_for(node, user_id)})"
    )


async def test_modifies_from_stale_copies_racing_every_status_writer_converge_on_the_node(monkeypatch):
    failures = []
    for round_number in range(6):
        rng = random.Random(round_number)
        writers = asyncio.Lock()
        quiet(monkeypatch, writers)
        tag = unique_name("race_tag")
        group_id = await seed_group(tag)
        expiring = [await seed_user(group_id, expire=dt.now(UTC) - td(seconds=1)) for _ in range(3)]
        limited = [await seed_user(group_id) for _ in range(3)]
        plain = [await seed_user(group_id) for _ in range(3)]
        everyone = expiring + limited + plain
        node = await node_with(monkeypatch, 2800 + round_number, dict.fromkeys(everyone, tag))
        operator = UserOperation(OperatorType.API)

        async def stale_modify(user_id, change, delay, writers=writers, operator=operator):
            async with TestSession() as db:
                loaded = await get_user_by_id(db, user_id)
                await asyncio.sleep(delay)
                async with writers:
                    await operator._modify_user(db, loaded, change, OWNER)

        async def expire_job(delay, writers=writers):
            await asyncio.sleep(delay)
            async with writers:
                await review_users.expire_users_job()

        async def limiter(user_id, delay, writers=writers):
            await asyncio.sleep(delay)
            async with writers:
                await over_limit(user_id)

        async def bulk_disable(user_ids, delay, writers=writers, operator=operator):
            await asyncio.sleep(delay)
            async with writers, TestSession() as db:
                await operator.bulk_disable_users(db, BulkUsersSelection(ids=set(user_ids)), OWNER)

        renew = UserModify(status=UserStatus.active, expire=dt.now(UTC) + td(days=30))
        actions = [expire_job(rng.choice((0, 0.02, 0.05)))]
        actions += [limiter(user_id, rng.choice((0, 0.02, 0.05))) for user_id in limited]
        actions += [stale_modify(user_id, renew, rng.choice((0.05, 0.1, 0.2))) for user_id in expiring]
        actions += [
            stale_modify(user_id, UserModify(status=UserStatus.active), rng.choice((0.05, 0.1, 0.2)))
            for user_id in limited
        ]
        actions += [
            stale_modify(user_id, UserModify(note=f"r{round_number}"), rng.choice((0, 0.05, 0.1))) for user_id in plain
        ]
        actions.append(bulk_disable(rng.sample(plain, 1), rng.choice((0, 0.05, 0.1))))
        rng.shuffle(actions)
        try:
            await asyncio.gather(*actions)
            await drained(node)
            async with TestSession() as session:
                rows = (await session.execute(select(User.id, User.status).where(User.id.in_(everyone)))).all()
            serving = {str(row.id) for row in rows if row.status in SERVING}
            on_node = {email for email in node.live if email in {str(user_id) for user_id in everyone}}
            if on_node != serving:
                failures.append(
                    f"round {round_number}: node serves {sorted(on_node)} but the database serves {sorted(serving)}; "
                    f"statuses {sorted((row.id, row.status.value) for row in rows)}"
                )
        finally:
            await node.disconnect()
            await asyncio.sleep(0.5)
    assert failures == []
