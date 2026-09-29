import asyncio
import importlib
import importlib.util
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PasarGuardNodeBridge.storage import get_default_lifecycle_coordinator
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

import app.node.bridge as node_bridge
import app.operation.user as user_operation
from app import notification
from app.db.models import User, UserStatus
from app.jobs import node_checker
from app.models.user import BulkUsersSelection, UserModify
from app.nats.node_rpc import node_nats_client
from app.node import node_manager, sync as node_sync, worker as node_worker
from app.node.bridge import _QueuedBatchSync
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from app.operation.user import UserOperation
from config import runtime_settings
from role import Role
from tests.a1_bridge_harness import RecordingNode, TestGetDB, isolate_node_manager, seed_group, seed_user
from tests.api import GetTestDB, TestSession
from tests.test_node_connect_stale_snapshot import Harness, _bulk, eventually

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)


def quiet_side_effects(monkeypatch):
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("remove_user", "modify_user", "create_user", "user_status_change"):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(node_operation.notification, "remove_node", AsyncMock())


async def user_exists(user_id: int) -> bool:
    async with TestSession() as session:
        return (await session.execute(select(User.id).where(User.id == user_id))).first() is not None


async def delete_user_through_the_api_operation(user_id: int):
    async with TestSession() as db:
        return await UserOperation(OperatorType.API).remove_user_by_id(db, user_id, OWNER)


async def node_process_maintenance(monkeypatch, db_node=None, pg_node=None) -> None:
    removals = importlib.import_module("app.fork.node_removals")
    monkeypatch.setattr(removals, "GetDB", GetTestDB)
    if db_node is not None:
        monkeypatch.setattr(node_checker, "GetDB", GetTestDB)
        monkeypatch.setattr(node_checker, "after_healthy_node_check", AsyncMock())
        monkeypatch.setattr(NodeOperation, "_update_single_node_status", AsyncMock())
        await node_checker.process_node_health_check(db_node, pg_node)
    await removals.drain_node_removals()


def backend_role_sync_module(monkeypatch):
    original_role = runtime_settings.role
    refresh_handler = node_bridge._refresh_handler
    monkeypatch.setattr(runtime_settings, "role", Role.BACKEND)
    spec = importlib.util.spec_from_file_location("outbox_backend_role_node_sync", node_sync.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(runtime_settings, "role", original_role)
    monkeypatch.setattr(node_bridge, "_refresh_handler", refresh_handler)
    assert not Role.BACKEND.runs_node
    return module


def node_worker_service():
    service = node_worker.NodeWorkerService.__new__(node_worker.NodeWorkerService)
    service._command_semaphore = asyncio.Semaphore(10)
    service._command_handlers = {"update_users": service._update_users, "update_user": service._update_user}
    return service


class NatsLinkToTheNodeWorker:
    def __init__(self, service, failures: int):
        self.service = service
        self.failures = failures
        self.refused: list[bytes] = []
        self.delivered: list[bytes] = []

    async def publish(self, subject, data):
        if self.failures > 0:
            self.failures -= 1
            self.refused.append(data)
            raise ConnectionError("nats: connection closed")
        self.delivered.append(data)
        await self.service._handle_command(SimpleNamespace(data=data))


def crash_after_commit(monkeypatch) -> list:
    lost: list = []

    async def process_died_before_this_ran(proto_users):
        lost.append([user.email for user in proto_users])

    monkeypatch.setattr(node_sync, "_dispatch_users_update", process_died_before_this_ran)
    return lost


async def test_a_removal_whose_node_enqueue_failed_once_is_retried_by_the_node_process(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        pg_node = await h.boot(db_node)
        store = pg_node._user_sync_store
        real_enqueue = store.enqueue_users
        refused: list[list[str]] = []

        async def enqueue_fails_once(node_id, users):
            if not refused:
                refused.append([user.email for user in users])
                raise RuntimeError(f"failed to enqueue NATS KV key=node.{node_id} after CAS retries")
            return await real_enqueue(node_id, users)

        monkeypatch.setattr(store, "enqueue_users", enqueue_fails_once)

        assert await delete_user_through_the_api_operation(h.user_id) == {}
        assert await eventually(lambda: refused), "the removal was never handed to the node"
        assert not await user_exists(h.user_id)

        await node_process_maintenance(monkeypatch, db_node, pg_node)

        assert await h.cleared(db_node), (
            f"user {h.email} was deleted, its one removal push failed to enqueue, and after the node process's "
            "maintenance pass the connected node still serves it"
        )


async def test_a_backend_role_removal_lost_to_a_failed_nats_publish_is_delivered_by_the_node_process(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        pg_node = await h.boot(db_node)
        backend = backend_role_sync_module(monkeypatch)
        link = NatsLinkToTheNodeWorker(node_worker_service(), failures=1)
        monkeypatch.setattr(node_nats_client, "_get_client", AsyncMock(return_value=link))
        monkeypatch.setattr(user_operation, "sync_remove_users", backend.remove_users)

        assert await delete_user_through_the_api_operation(h.user_id) == {}
        assert await eventually(lambda: link.refused)
        assert link.delivered == []

        await node_process_maintenance(monkeypatch, db_node, pg_node)

        assert await h.cleared(db_node), (
            f"user {h.email} was deleted in the backend role, its only update_users publish failed, and the node "
            "process never removed it"
        )


async def test_a_removal_lost_to_a_crash_of_the_committing_process_is_delivered_by_the_surviving_node_process(
    monkeypatch,
):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        pg_node = await h.boot(db_node)
        lost = crash_after_commit(monkeypatch)

        assert await delete_user_through_the_api_operation(h.user_id) == {}
        assert await eventually(lambda: lost)
        assert not await user_exists(h.user_id)

        await node_process_maintenance(monkeypatch, db_node, pg_node)

        assert await h.cleared(db_node), (
            f"user {h.email} was committed as deleted, the committing process died before dispatching the removal, "
            "and the surviving node process never removed it"
        )


@pytest.mark.parametrize("lifecycle", ["attach", "start"])
async def test_a_restart_after_a_crash_between_commit_and_dispatch_removes_the_deleted_user(monkeypatch, lifecycle):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        db_node = h.node(1)
        await h.boot(db_node)
        lost = crash_after_commit(monkeypatch)

        assert await delete_user_through_the_api_operation(h.user_id) == {}
        assert await eventually(lambda: lost)
        assert h.remote(db_node).carries(h.email)

        await h.restart_panel(db_node)
        if lifecycle == "start":
            get_default_lifecycle_coordinator()._states.pop(str(db_node.id), None)
        await _bulk([db_node], force_start=False)

        assert await h.cleared(db_node), (
            f"{lifecycle}: the startup connect kept serving a user deleted before the crash"
        )


async def test_a_delete_whose_removal_cannot_be_recorded_is_not_committed(monkeypatch):
    quiet_side_effects(monkeypatch)
    removals = importlib.import_module("app.fork.crud.node_removals")
    group_id = await seed_group("outbox_refused")
    user_id = await seed_user(group_id)

    async def recording_fails(db, user_ids):
        raise OperationalError("INSERT INTO pending_node_removals", {}, Exception("disk I/O error"))

    monkeypatch.setattr(removals, "mark_node_removals", recording_fails)
    with pytest.raises(OperationalError):
        await delete_user_through_the_api_operation(user_id)

    assert await user_exists(user_id), "the user row was deleted although its node removal could not be recorded"


class QueuedRecordingNode(_QueuedBatchSync, RecordingNode):
    pass


class ReaderBehindWriters(node_sync.CurrentStateReader):
    def __init__(self, writers: asyncio.Lock):
        super().__init__()
        self._writers = writers

    async def _query(self, user_ids):
        async with self._writers:
            return await super()._query(user_ids)


async def drained(node, timeout=15.0):
    async with asyncio.timeout(timeout):
        while True:
            await asyncio.sleep(0.2)
            if not await node._user_sync_store.has_pending(node.node_id):
                await asyncio.sleep(0.3)
                if not await node._user_sync_store.has_pending(node.node_id):
                    return


async def test_deletes_losing_their_dispatch_at_random_converge_on_the_node_through_the_outbox(monkeypatch):
    quiet_side_effects(monkeypatch)
    removals = importlib.import_module("app.fork.node_removals")
    monkeypatch.setattr(removals, "GetDB", TestGetDB)
    monkeypatch.setattr(node_sync, "GetDB", TestGetDB)
    monkeypatch.setattr(node_bridge, "_refresh_handler", node_sync.refresh_node_users)
    real_dispatch = node_sync._dispatch_users_update
    operator = UserOperation(OperatorType.API)
    failures = []
    for round_number in range(6):
        rng = random.Random(round_number)
        writers = asyncio.Lock()
        monkeypatch.setattr(node_sync, "_current_state", ReaderBehindWriters(writers))
        tag = f"outbox_{round_number}_{rng.randrange(10**6)}"
        group_id = await seed_group(tag)
        ids = [await seed_user(group_id) for _ in range(8)]
        node = QueuedRecordingNode(3500 + round_number)
        await node.connect("0.6.4", "26.3.27")
        node.live.update({str(user_id): [tag] for user_id in ids})
        isolate_node_manager(monkeypatch, node_manager, {3500 + round_number: node})

        async def flaky_dispatch(proto_users, rng=rng):
            if rng.random() < 0.5:
                return
            await real_dispatch(proto_users)

        monkeypatch.setattr(node_sync, "_dispatch_users_update", flaky_dispatch)
        victims = rng.sample(ids, 4)
        survivors = [user_id for user_id in ids if user_id not in victims]

        async def delete_one(user_id, delay, writers=writers):
            await asyncio.sleep(delay)
            async with writers, TestSession() as db:
                await operator.remove_user_by_id(db, user_id, OWNER)

        async def delete_bulk(user_ids, delay, writers=writers):
            await asyncio.sleep(delay)
            async with writers, TestSession() as db:
                await operator.bulk_remove_users(db, BulkUsersSelection(ids=set(user_ids)), OWNER)

        async def edit(user_id, delay, writers=writers, round_number=round_number):
            await asyncio.sleep(delay)
            async with writers, TestSession() as db:
                await operator.modify_user_by_id(db, user_id, UserModify(note=f"r{round_number}"), OWNER)

        async def maintenance(delay):
            await asyncio.sleep(delay)
            await removals.drain_node_removals()

        actions = [delete_one(victims[0], rng.choice((0, 0.02, 0.05))), delete_one(victims[1], rng.choice((0, 0.05)))]
        actions.append(delete_bulk(victims[2:], rng.choice((0, 0.02, 0.05))))
        actions += [edit(user_id, rng.choice((0, 0.01, 0.05))) for user_id in survivors]
        actions += [maintenance(rng.choice((0, 0.03, 0.08))) for _ in range(2)]
        rng.shuffle(actions)
        try:
            await asyncio.gather(*actions)
            await removals.drain_node_removals()
            await drained(node)
            async with TestSession() as session:
                rows = (await session.execute(select(User.id, User.status).where(User.id.in_(ids)))).all()
            serving = {str(row.id) for row in rows if row.status in (UserStatus.active, UserStatus.on_hold)}
            on_node = {email for email in node.live if email in {str(user_id) for user_id in ids}}
            if on_node != serving:
                failures.append(f"round {round_number}: node serves {sorted(on_node)}, database {sorted(serving)}")
        finally:
            monkeypatch.setattr(node_sync, "_dispatch_users_update", real_dispatch)
            await node.disconnect()
            await asyncio.sleep(0.5)
    assert failures == []
