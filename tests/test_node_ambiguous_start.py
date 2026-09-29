import asyncio
import importlib
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PasarGuardNodeBridge import Health, NodeAPIError
from sqlalchemy import select

from app import notification
from app.db.models import NodeStatus, User, UserStatus, users_groups_association
from app.jobs import node_checker
from app.models.user import UserModify
from app.node import node_manager, sync as node_sync
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from app.operation.user import UserOperation
from tests.api import GetTestDB, TestSession
from tests.api.helpers import unique_name
from tests.test_node_connect_stale_snapshot import SETTINGS, Harness, Remote, _bulk

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)


def quiet_side_effects(monkeypatch):
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("remove_user", "modify_user", "create_user", "user_status_change"):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(node_operation.notification, "remove_node", AsyncMock())


async def delete_user(user_id: int):
    async with TestSession() as db:
        return await UserOperation(OperatorType.API).remove_user_by_id(db, user_id, OWNER)


async def health_tick(monkeypatch, db_node, pg_node) -> None:
    monkeypatch.setattr(node_checker, "GetDB", GetTestDB)
    monkeypatch.setattr(node_checker, "after_healthy_node_check", AsyncMock())
    monkeypatch.setattr(NodeOperation, "_update_single_node_status", AsyncMock())
    await node_checker.process_node_health_check(db_node, pg_node)


def lost_dispatch(monkeypatch) -> None:
    async def process_died_before_this_ran(proto_users):
        return None

    monkeypatch.setattr(node_sync, "_dispatch_users_update", process_died_before_this_ran)


async def seed_user_in_the_same_group(user_id: int) -> int:
    async with TestSession() as session:
        group_id = (
            await session.execute(
                select(users_groups_association.c.groups_id).where(users_groups_association.c.user_id == user_id)
            )
        ).scalar_one()
        user = User(username=unique_name("late_start_user"), proxy_settings=SETTINGS, status=UserStatus.active)
        session.add(user)
        await session.flush()
        await session.execute(users_groups_association.insert(), [{"user_id": user.id, "groups_id": group_id}])
        await session.commit()
        return user.id


class LateStartRemote(Remote):
    def __init__(self):
        super().__init__()
        self.defer_start = False
        self.pending_start = None

    async def rpc(self, name: str, request):
        if name == "Start" and self.defer_start:
            await asyncio.sleep(0)
            self.pending_start = request
            raise NodeAPIError(-1, "Request timed out")
        return await super().rpc(name, request)

    def apply_late_start(self) -> None:
        request, self.pending_start = self.pending_start, None
        self.running = True
        self.backends = {request.type: self._backend(request)}


def late_start_node(h):
    db_node = h.node(1)
    h.remotes[db_node.id] = LateStartRemote()
    return db_node


async def test_a_timed_out_force_start_applied_after_a_newer_removal_is_superseded_by_a_resync(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        db_node = late_start_node(h)
        pg_node = await h.boot(db_node)
        remote = h.remote(db_node)
        remote.lost = {"Stop"}
        remote.defer_start = True

        await _bulk([db_node], force_start=True)
        assert remote.pending_start is not None
        assert h.email in {user.email for user in remote.pending_start.users}
        assert await pg_node.get_health() is Health.HEALTHY

        assert await delete_user(h.user_id) == {}
        assert await h.cleared(db_node), "precondition: the removal delta reached the attached core"

        remote.apply_late_start()
        assert remote.carries(h.email)
        await health_tick(monkeypatch, db_node, pg_node)

        assert await h.cleared(db_node), (
            f"the Start that timed out carried user {h.email}; the node applied it after the newer removal, and the "
            "panel never sent a newer snapshot, so the deleted user is served again"
        )


@pytest.mark.parametrize("removal", ["queued", "lost"])
async def test_a_timed_out_start_applied_before_the_panel_attaches_is_superseded_by_the_health_check_resync(
    monkeypatch, removal
):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        db_node = late_start_node(h)
        db_node.status = NodeStatus.error
        remote = h.remote(db_node)
        remote.defer_start = True

        await _bulk([db_node], force_start=False)
        pg_node = await node_manager.get_node(db_node.id)
        assert remote.pending_start is not None and not remote.running

        if removal == "lost":
            lost_dispatch(monkeypatch)
        assert await delete_user(h.user_id) == {}
        remote.defer_start = False
        remote.apply_late_start()
        assert remote.carries(h.email)

        await health_tick(monkeypatch, db_node, pg_node)

        assert await h.cleared(db_node), "the late Start's stale snapshot survived the health-check attach"


async def test_a_timed_out_start_that_lands_after_the_health_check_attached_is_superseded_on_the_next_tick(
    monkeypatch,
):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        db_node = late_start_node(h)
        db_node.status = NodeStatus.error
        remote = h.remote(db_node)
        remote.defer_start = True

        await _bulk([db_node], force_start=False)
        pg_node = await node_manager.get_node(db_node.id)
        stale = remote.pending_start
        assert stale is not None and not remote.running

        remote.defer_start = False
        remote.running = True
        remote.backends = {stale.type: remote._backend(stale)}
        await health_tick(monkeypatch, db_node, pg_node)
        db_node.status = NodeStatus.connected

        assert await delete_user(h.user_id) == {}
        assert await h.cleared(db_node), "precondition: the removal reached the attached core"

        remote.pending_start = stale
        remote.apply_late_start()
        assert remote.carries(h.email)
        await health_tick(monkeypatch, db_node, pg_node)

        assert await h.cleared(db_node), (
            "a Start that timed out with nothing to attach to landed after the health check attached and resynced, "
            f"and user {h.email}, deleted in between, is served again"
        )


class ReaderBehindWriters(node_sync.CurrentStateReader):
    def __init__(self, writers: asyncio.Lock):
        super().__init__()
        self._writers = writers

    async def _query(self, user_ids):
        async with self._writers:
            return await super()._query(user_ids)


async def late_start_round(monkeypatch, round_number: int) -> str | None:
    rng = random.Random(round_number)
    operator = UserOperation(OperatorType.API)
    writers = asyncio.Lock()
    async with Harness(monkeypatch) as h:
        monkeypatch.setattr(node_sync, "_current_state", ReaderBehindWriters(writers))
        db_node = late_start_node(h)
        others = [await seed_user_in_the_same_group(h.user_id) for _ in range(3)]
        pg_node = await h.boot(db_node)
        remote = h.remote(db_node)
        remote.lost = {"Stop"}
        remote.defer_start = True
        await _bulk([db_node], force_start=True)
        remote.defer_start = False
        doomed = [h.user_id, *rng.sample(others, 1)]
        disabled = [user_id for user_id in others if user_id not in doomed][:1]

        async def delete_later(user_id, delay):
            await asyncio.sleep(delay)
            async with writers:
                await delete_user(user_id)

        async def disable_later(user_id, delay):
            await asyncio.sleep(delay)
            async with writers, TestSession() as db:
                await operator.modify_user_by_id(db, user_id, UserModify(status=UserStatus.disabled), OWNER)

        async def late_start(delay):
            await asyncio.sleep(delay)
            remote.apply_late_start()

        async def tick(delay):
            await asyncio.sleep(delay)
            async with writers:
                await health_tick(monkeypatch, db_node, pg_node)

        actions = [delete_later(user_id, rng.choice((0, 0.05, 0.15))) for user_id in doomed]
        actions += [disable_later(user_id, rng.choice((0, 0.05, 0.15))) for user_id in disabled]
        actions.append(late_start(rng.choice((0.01, 0.1, 0.3))))
        actions += [tick(rng.choice((0, 0.1, 0.2, 0.4))) for _ in range(3)]
        rng.shuffle(actions)
        await asyncio.gather(*actions)
        await asyncio.sleep(0.7)
        await health_tick(monkeypatch, db_node, pg_node)
        await asyncio.sleep(0.3)
        async with TestSession() as session:
            rows = (await session.execute(select(User.id, User.status).where(User.id.in_([h.user_id, *others])))).all()
        serving = {str(row.id) for row in rows if row.status in (UserStatus.active, UserStatus.on_hold)}
        carried = {str(user_id) for user_id in [h.user_id, *others] if remote.carries(str(user_id))}
        if carried != serving:
            return f"round {round_number}: node carries {sorted(carried)}, database {sorted(serving)}"
    return None


async def test_late_starts_racing_deletes_edits_and_health_ticks_converge_after_the_window(monkeypatch):
    quiet_side_effects(monkeypatch)
    extras = importlib.import_module("app.fork.operation.node_extras")
    monkeypatch.setattr(extras, "ambiguous_start_window", lambda db_node: 0.6)
    failures = []
    for round_number in range(5):
        with monkeypatch.context() as round_patch:
            failure = await late_start_round(round_patch, round_number)
        if failure:
            failures.append(failure)
    assert failures == []
