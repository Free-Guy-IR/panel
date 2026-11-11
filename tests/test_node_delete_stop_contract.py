import asyncio
import dataclasses
import importlib
import random
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PasarGuardNodeBridge import NodeAPIError
from PasarGuardNodeBridge.storage import (
    LifecycleOperation,
    LifecycleStatus,
    get_default_lifecycle_coordinator,
    get_default_user_sync_store,
)
from sqlalchemy import event, inspect, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import notification
from app.db.models import Node, NodeConnectionType
from app.fork.operation import node_extras
from app.jobs import node_checker
from app.models.node import BulkNodeSelection
from app.nats.node_rpc import node_nats_client
from app.node import manager_sync, node_manager, sync as node_sync, worker as node_worker
from app.operation import OperatorType, node as node_operation
from app.operation.node import NodeOperation
from app.operation.user import UserOperation
from config import runtime_settings
from role import Role
from tests.api import DATABASE_URL, IS_SQLITE, TestSession
from tests.api.helpers import unique_name
from tests.test_node_connect_stale_snapshot import Harness, _ca_pem, _operation

ADMIN = SimpleNamespace(username="admin")
EVERY_RPC = {
    "Start",
    "Stop",
    "GetBaseInfo",
    "GetBackendStats",
    "SyncUsers",
    "AddBackend",
    "RemoveBackend",
    "ListBackends",
}
ROUNDS = 24
RETIREMENT = "app.fork.operation.node_retirement"


def retirement():
    return importlib.import_module(RETIREMENT)


def retirement_if_present():
    try:
        return retirement()
    except ModuleNotFoundError:
        return None


@pytest.fixture(autouse=True)
def single_process_bridge_memory(monkeypatch):
    from app.node import nats_memory

    monkeypatch.setattr(nats_memory, "_user_sync_store", None)
    monkeypatch.setattr(nats_memory, "_lifecycle_coordinator", None)
    modules = [nats_memory, node_operation, node_checker]
    present = retirement_if_present()
    if present is not None:
        modules.append(present)
    for module in modules:
        monkeypatch.setattr(module, "needs_shared_bridge_memory", lambda: False)


if IS_SQLITE:
    own_connections = create_async_engine(
        DATABASE_URL, connect_args={"check_same_thread": False, "uri": True}, poolclass=NullPool
    )

    @event.listens_for(own_connections.sync_engine, "connect")
    def _enforce_sqlite_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Session = async_sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=own_connections)
else:
    own_connections = create_async_engine(DATABASE_URL, poolclass=NullPool)
    Session = async_sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=own_connections)


class SessionOfItsOwn:
    def __init__(self):
        self.db = Session()

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc_info):
        await self.db.close()


def quiet_side_effects(monkeypatch):
    for module in (node_checker, manager_sync, node_sync):
        monkeypatch.setattr(module, "GetDB", SessionOfItsOwn, raising=False)
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("remove_user", "modify_user", "create_user", "user_status_change"):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(node_operation.notification, "remove_node", AsyncMock())
    monkeypatch.setattr(node_extras, "_CONNECT_LOCKS", {})
    present = retirement_if_present()
    if present is not None:
        monkeypatch.setattr(present, "GetDB", SessionOfItsOwn)


async def node_row_exists(node_id: int) -> bool:
    async with Session() as session:
        return (await session.execute(select(Node.id).where(Node.id == node_id))).first() is not None


async def owed_table_present(session) -> bool:
    return await session.run_sync(lambda sync: inspect(sync.connection()).has_table("node_retirements"))


async def stops_owed_to(address: str) -> int:
    async with Session() as session:
        if not await owed_table_present(session):
            return 0
        query = text("SELECT count(*) FROM node_retirements WHERE address = :address")
        return (await session.execute(query, {"address": address})).scalar_one()


async def owed_rows(address: str) -> list:
    async with Session() as session:
        if not await owed_table_present(session):
            return []
        query = text(
            "SELECT attempts, next_attempt_at, last_error, reason FROM node_retirements WHERE address = :address"
        )
        return list((await session.execute(query, {"address": address})).all())


async def forget_every_owed_stop() -> None:
    async with Session() as session:
        if await owed_table_present(session):
            await session.execute(text("DELETE FROM node_retirements"))
            await session.commit()


async def drain_owed_stops(monkeypatch, now: datetime) -> None:
    module = retirement()
    monkeypatch.setattr(module, "GetDB", SessionOfItsOwn)
    await module.drain_node_retirements(now=now)


async def seed_node_row(address: str | None = None, api_port: int = 1, port: int = 1) -> Node:
    async with Session() as session:
        row = Node(
            name=unique_name("retire_node"),
            address=address or f"{unique_name('retire')}.nodes.test",
            port=port,
            api_port=api_port,
            server_ca=_ca_pem(),
            api_key=str(uuid4()),
            core_config_id=None,
        )
        row.connection_type = NodeConnectionType.grpc
        session.add(row)
        await session.commit()
        return row


def forget_bridge_state(node_id: int) -> None:
    coordinator = get_default_lifecycle_coordinator()
    coordinator._states.pop(str(node_id), None)
    coordinator._leases.pop(str(node_id), None)


async def managed_row(h, monkeypatch, *, row_backed: bool = False):
    await forget_every_owed_stop()
    row = await seed_node_row()
    forget_bridge_state(row.id)
    await get_default_user_sync_store().clear(str(row.id))
    db_node = h.node(1)
    h.db_nodes.pop(db_node.id)
    remote = h.remotes.pop(db_node.id)
    db_node.id = row.id
    db_node.address = row.address
    h.db_nodes[row.id] = db_node
    h.remotes[row.id] = remote
    if row_backed:

        async def lookup(_db, node_id, **_kwargs):
            if not await node_row_exists(node_id):
                return None
            return h.db_nodes.get(node_id)

        monkeypatch.setattr(node_operation, "get_node_by_id", AsyncMock(side_effect=lookup))
    await h.boot(db_node)
    return row, db_node


def backend_role_operation(monkeypatch, operator_type: OperatorType = OperatorType.API) -> NodeOperation:
    current = runtime_settings.role
    monkeypatch.setattr(runtime_settings, "role", Role.BACKEND)
    try:
        return NodeOperation(operator_type=operator_type)
    finally:
        monkeypatch.setattr(runtime_settings, "role", current)


async def delete(row, *, bulk: bool, operator_type: OperatorType = OperatorType.SYSTEM, operation=None):
    operation = operation or NodeOperation(operator_type=operator_type)
    async with Session() as db:
        if bulk:
            return await operation.bulk_remove_nodes(db, BulkNodeSelection(ids={row.id}), ADMIN)
        return await operation.remove_node(db, row.id, ADMIN)


async def refused_delete(row, *, bulk: bool, operator_type: OperatorType = OperatorType.SYSTEM, operation=None):
    try:
        await delete(row, bulk=bulk, operator_type=operator_type, operation=operation)
    except (HTTPException, ValueError) as exc:
        return exc
    return None


def stop_fails_once(remote) -> None:
    remote.lost.add("Stop")

    async def heal_on_the_next_stop():
        remote.lost.discard("Stop")

    async def arm():
        remote.on("Stop", heal_on_the_next_stop)

    remote.on("Stop", arm)


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_deleting_a_node_whose_stop_fails_leaves_its_core_serving_every_user_with_nothing_managing_it(
    monkeypatch, bulk
):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = {"Stop"}

        refusal = await refused_delete(row, bulk=bulk)
        await asyncio.sleep(0.5)

        row_kept = await node_row_exists(row.id)
        managed = await node_manager.get_node(row.id) is not None
        assert row_kept or not remote.running, (
            f"node {row.id} was deleted (row kept={row_kept}, still managed={managed}) although its Stop failed; "
            f"the core is still running and serving {sorted(remote.backends[next(iter(remote.backends))]['users'])}"
        )
        assert refusal is not None, "the node row was kept but the delete reported success"
        assert managed, "the refused delete kept the row but left nothing managing the running core"
        if not row_kept:
            h.db_nodes.pop(row.id)


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_a_refused_delete_answers_503_over_the_api_and_keeps_pushing_user_changes(monkeypatch, bulk):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = {"Stop"}

        refusal = await refused_delete(row, bulk=bulk, operator_type=OperatorType.API)

        assert isinstance(refusal, HTTPException) and refusal.status_code == 503, refusal
        assert "still running" in refusal.detail
        assert await node_row_exists(row.id)
        assert await stops_owed_to(row.address) == 0
        await h.disable()
        assert await h.cleared(db_node), "a user change after the refused delete never reached the kept node"


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
async def test_deleting_a_reachable_node_stops_its_core_before_the_row_goes(monkeypatch, bulk):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        row_present_at_stop: list[bool] = []

        async def note_the_row():
            row_present_at_stop.append(await node_row_exists(row.id))

        remote.on("Stop", note_the_row)
        result = await delete(row, bulk=bulk)
        await asyncio.sleep(0.1)

        assert row_present_at_stop == [True], "the row was deleted before its core was told to stop"
        assert not remote.running
        assert not await node_row_exists(row.id)
        assert await node_manager.get_node(row.id) is None
        assert await stops_owed_to(row.address) == 0
        if bulk:
            assert result.count == 1 and result.nodes == [row.name]
        h.db_nodes.pop(row.id)


async def test_a_stop_that_fails_once_is_retried_before_the_delete_is_refused(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        stop_fails_once(remote)

        assert await refused_delete(row, bulk=False) is None
        await asyncio.sleep(0.1)
        assert not remote.running, "a Stop that failed once was never retried and the deleted node's core still runs"
        assert not await node_row_exists(row.id)
        h.db_nodes.pop(row.id)


async def test_an_unreachable_node_is_deleted_and_its_owed_stop_lands_when_it_answers_again(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)

        assert await refused_delete(row, bulk=False) is None
        await asyncio.sleep(0.1)
        h.db_nodes.pop(row.id)

        assert not await node_row_exists(row.id)
        assert await node_manager.get_node(row.id) is None
        assert remote.running
        assert await stops_owed_to(row.address) == 1, (
            "the unreachable node was deleted with its core still serving every user and nothing owes it a Stop"
        )

        now = datetime.now(UTC)
        await drain_owed_stops(monkeypatch, now + timedelta(minutes=5))
        assert remote.running and await stops_owed_to(row.address) == 1

        remote.lost = set()
        await drain_owed_stops(monkeypatch, now + timedelta(minutes=5))
        assert remote.running, "a retry ran before its backoff elapsed"
        await drain_owed_stops(monkeypatch, now + timedelta(hours=3))

        assert not remote.running, "the node answered again but the owed Stop was never sent"
        assert await stops_owed_to(row.address) == 0


async def test_an_owed_stop_still_reaches_a_server_whose_address_is_a_node_again_on_another_service_port(
    monkeypatch,
):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)

        await seed_node_row(address=row.address, api_port=row.api_port, port=row.port + 1)
        remote.lost = set()
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(hours=1))

        assert not remote.running, "a node on another service port of the same host cancelled the owed Stop"
        assert await stops_owed_to(row.address) == 0


async def test_an_owed_stop_is_dropped_for_a_server_added_back_on_its_service_port_with_another_api_port(
    monkeypatch,
):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)
        assert await stops_owed_to(row.address) == 1

        await seed_node_row(address=row.address, api_port=row.api_port + 1, port=row.port)
        remote.lost = set()
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(hours=1))

        assert remote.running, "the owed Stop killed the core of the node that now owns that service port"
        assert await stops_owed_to(row.address) == 0


async def test_an_owed_stop_is_dropped_for_a_server_added_back_after_the_claim_and_before_the_stop(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)
        assert await stops_owed_to(row.address) == 1

        module = retirement()
        real_check = module._server_is_a_node_again
        checks = []

        async def re_added_right_after_the_first_check(owed):
            found = await real_check(owed)
            checks.append(found)
            if len(checks) == 1:
                await seed_node_row(address=row.address, api_port=row.api_port, port=row.port)
            return found

        monkeypatch.setattr(module, "_server_is_a_node_again", re_added_right_after_the_first_check)
        remote.lost = set()
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(hours=1))

        assert checks[0] is False
        assert remote.running, "a server added back between the claim and the Stop had its core stopped"
        assert await stops_owed_to(row.address) == 0


async def test_an_owed_stop_is_given_up_after_its_retention(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)
        assert await stops_owed_to(row.address) == 1

        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(days=8))

        assert await stops_owed_to(row.address) == 0


async def test_two_drainers_attempt_one_owed_stop_once(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)
        assert await stops_owed_to(row.address) == 1
        remote.lost = set()
        stops = []
        real_rpc = remote.rpc

        async def counting_rpc(name, request):
            if name == "Stop":
                stops.append(name)
            return await real_rpc(name, request)

        remote.rpc = counting_rpc
        later = datetime.now(UTC) + timedelta(hours=1)
        await asyncio.gather(drain_owed_stops(monkeypatch, later), drain_owed_stops(monkeypatch, later))

        assert len(stops) == 1
        assert not remote.running
        assert await stops_owed_to(row.address) == 0


async def test_shared_workers_recheck_a_confirmed_stop_after_a_sibling_start_could_have_landed(monkeypatch):
    quiet_side_effects(monkeypatch)
    module = retirement()
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        monkeypatch.setattr(module, "needs_shared_bridge_memory", lambda: True)
        monkeypatch.setattr(module, "publish_node_sync", AsyncMock())

        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)
        assert not remote.running
        assert await stops_owed_to(row.address) == 1
        module.publish_node_sync.assert_awaited_once_with("remove", row.id)

        remote.running = True
        now = datetime.now(UTC)
        await drain_owed_stops(monkeypatch, now + timedelta(seconds=30))
        assert remote.running, "the settle recheck ran before a sibling's in-flight Start could have finished"

        await drain_owed_stops(monkeypatch, now + timedelta(minutes=10))
        assert not remote.running, "a sibling's late Start after the delete kept serving users"
        assert await stops_owed_to(row.address) == 0


async def test_a_delete_after_a_start_that_timed_out_rechecks_the_core_once_that_start_could_have_landed(
    monkeypatch,
):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        node_extras.note_ambiguous_start(db_node)

        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)
        assert not remote.running
        assert await stops_owed_to(row.address) == 1, (
            "a Start that timed out can still be applied by the node after the delete's Stop, and nothing rechecks it"
        )

        remote.running = True
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(minutes=10))

        assert not remote.running, "the timed-out Start landed after the delete and its core kept serving users"
        assert await stops_owed_to(row.address) == 0


async def test_a_backend_role_delete_owes_the_stop_in_the_same_transaction_and_a_node_process_delivers_it(
    monkeypatch,
):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        published: list[tuple[str, dict]] = []

        async def publish(action, payload):
            published.append((action, payload))

        async def no_worker_answer_is_awaited(*args, **kwargs):
            raise AssertionError("the API process waited for a node worker answer to decide the delete")

        monkeypatch.setattr(node_nats_client, "publish", publish)
        monkeypatch.setattr(node_nats_client, "request", no_worker_answer_is_awaited)
        operation = backend_role_operation(monkeypatch)

        refusal = await refused_delete(row, bulk=False, operation=operation)

        assert refusal is None, refusal
        assert not await node_row_exists(row.id)
        assert await stops_owed_to(row.address) == 1, (
            "the backend role deleted the node row and only published remove_node; if that publish or its Stop is "
            "lost, nothing stops the core"
        )
        assert published == [("remove_node", {"node_id": row.id})]
        assert remote.running and await node_manager.get_node(row.id) is not None
        h.db_nodes.pop(row.id)

        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(seconds=1))

        assert not remote.running, "the node process never delivered the Stop the backend role owed"
        assert await node_manager.get_node(row.id) is None, "the node process still manages the deleted node"
        assert await stops_owed_to(row.address) == 1

        remote.running = True
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(minutes=10))

        assert not remote.running, "a Start the node worker had in flight landed after the Stop and kept serving"
        assert await stops_owed_to(row.address) == 0


@pytest.mark.parametrize("blocked_for", [0.3, 1.5])
async def test_a_backend_role_delete_answers_only_what_its_own_transaction_committed(monkeypatch, blocked_for):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        monkeypatch.setattr(node_nats_client, "publish", AsyncMock())
        operation = backend_role_operation(monkeypatch)
        blocker = Session()
        await blocker.execute(update(Node).where(Node.id == row.id).values(message="held by a concurrent writer"))
        answered = asyncio.Event()

        async def answer():
            try:
                return await refused_delete(row, bulk=False, operation=operation)
            finally:
                answered.set()

        deleting = asyncio.create_task(answer())
        await asyncio.sleep(blocked_for)
        answered_while_blocked = answered.is_set()
        await blocker.commit()
        await blocker.close()
        refusal = await deleting

        row_kept = await node_row_exists(row.id)
        owed = await stops_owed_to(row.address)
        if refusal is None:
            assert not answered_while_blocked, "the delete was answered before its transaction could commit"
            assert (row_kept, owed) == (False, 1), (row_kept, owed)
            h.db_nodes.pop(row.id)
        else:
            assert isinstance(refusal, HTTPException) and refusal.status_code == 503, refusal
            assert (row_kept, owed) == (True, 0), (
                f"the delete answered {refusal.status_code} but the database holds row={row_kept} owed={owed}"
            )


def round_plan(rng: random.Random):
    return SimpleNamespace(
        mode=rng.choice(["stop_works", "stop_fails", "stop_fails_once", "unreachable", "partition_heals"]),
        bulk=rng.random() < 0.5,
        backend=rng.random() < 0.25,
        lost_publish=rng.random() < 0.5,
        checks=rng.randint(1, 3),
        reconnects=rng.randint(1, 3),
        pushes=rng.randint(1, 4),
        jitter=[rng.random() * 0.03 for _ in range(32)],
        force=[rng.random() < 0.5 for _ in range(8)],
        single=[rng.random() < 0.5 for _ in range(8)],
        order=rng.sample(range(5), 5),
    )


@pytest.mark.parametrize("seed", range(ROUNDS))
async def test_node_delete_interleaved_with_health_checks_reconnects_and_user_pushes(monkeypatch, seed):
    rng = random.Random(seed)
    plan = round_plan(rng)
    jitter = iter(plan.jitter)
    quiet_side_effects(monkeypatch)
    monkeypatch.setattr(node_checker, "after_healthy_node_check", AsyncMock())
    monkeypatch.setattr(NodeOperation, "_update_single_node_status", AsyncMock())
    worker = node_worker.NodeWorkerService.__new__(node_worker.NodeWorkerService)
    deliveries: list[asyncio.Task] = []

    async def publish(action, payload):
        if action == "remove_node" and not plan.lost_publish:
            deliveries.append(asyncio.create_task(worker._remove_node(payload)))

    monkeypatch.setattr(node_nats_client, "publish", publish)
    operation = backend_role_operation(monkeypatch, OperatorType.SYSTEM) if plan.backend else None
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch, row_backed=True)
        remote = h.remote(db_node)
        if plan.mode == "stop_fails":
            remote.lost = {"Stop"}
        elif plan.mode == "stop_fails_once":
            stop_fails_once(remote)
        elif plan.mode in ("unreachable", "partition_heals"):
            remote.lost = set(EVERY_RPC)
            remote.stream_fails = True
        outcome: dict[str, object] = {}

        async def delete_it():
            await asyncio.sleep(next(jitter))
            outcome["refusal"] = await refused_delete(row, bulk=plan.bulk, operation=operation)

        async def health_checks():
            for _ in range(plan.checks):
                await asyncio.sleep(next(jitter))
                pg_node = await node_manager.get_node(row.id)
                if pg_node is not None:
                    await node_checker.process_node_health_check(db_node, pg_node)

        async def reconnects():
            for index in range(plan.reconnects):
                await asyncio.sleep(next(jitter))
                async with Session() as db:
                    if plan.single[index]:
                        await _operation().connect_single_node(db, row.id, force_start=plan.force[index])
                    else:
                        await _operation().connect_nodes_bulk(db, [db_node], force_start=plan.force[index])

        async def pushes():
            for index in range(plan.pushes):
                await asyncio.sleep(next(jitter))
                if plan.single[index]:
                    await node_manager.update_user(h.user([h.tag_a, h.tag_b]))
                else:
                    await node_manager.update_users([h.user([h.tag_a])])

        async def partition():
            await asyncio.sleep(next(jitter))
            if plan.mode == "partition_heals":
                remote.lost = set()
                remote.stream_fails = False

        actors = [delete_it, health_checks, reconnects, pushes, partition]
        await asyncio.gather(*(actors[index]() for index in plan.order))
        if deliveries:
            await asyncio.gather(*deliveries, return_exceptions=True)
        await asyncio.sleep(0.3)

        row_kept = await node_row_exists(row.id)
        managed = await node_manager.get_node(row.id) is not None
        owed = await stops_owed_to(row.address)
        context = (
            f"seed={seed} plan={plan.mode} bulk={plan.bulk} backend={plan.backend} "
            f"lost_publish={plan.lost_publish} order={plan.order}"
        )
        if row_kept:
            assert outcome["refusal"] is not None, f"{context}: the row survived a delete that reported success"
            assert managed, f"{context}: the delete was refused but nothing manages the running core"
            return
        h.db_nodes.pop(row.id)
        assert outcome["refusal"] is None, f"{context}: the delete was refused yet the row is gone"
        if not plan.backend or not plan.lost_publish:
            assert not managed, f"{context}: a node object is still managed for a deleted row"
        assert not remote.running or owed, (
            f"{context}: the row is gone, the core still serves {sorted(remote.backends)} and no Stop is owed"
        )

        remote.lost = set()
        remote.stream_fails = False
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(days=1))

        assert not remote.running, f"{context}: an owed Stop was forgotten"
        assert await node_manager.get_node(row.id) is None, f"{context}: the deleted node is still managed"
        assert await stops_owed_to(row.address) == 0, f"{context}: a confirmed Stop is still owed"


def as_utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def test_a_refused_delete_records_the_running_core_as_healthy_in_the_shared_lifecycle(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        h.remote(db_node).lost = {"Stop"}

        assert await refused_delete(row, bulk=False) is not None

        state = await get_default_lifecycle_coordinator().get_state(str(row.id))
        assert state is not None and state.observed is LifecycleStatus.HEALTHY, state


async def test_a_refused_delete_does_not_overwrite_a_lifecycle_operation_that_began_after_its_read(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        h.remote(db_node).lost = {"Stop"}
        coordinator = get_default_lifecycle_coordinator()
        real_get_state = coordinator.get_state
        sibling = []

        async def read_then_a_sibling_starts(node_id):
            state = await real_get_state(node_id)
            snapshot = None if state is None else dataclasses.replace(state)
            if node_id == str(row.id) and not sibling:
                sibling.append(await coordinator.try_acquire(node_id, "sibling", LifecycleOperation.START, 30))
            return snapshot

        monkeypatch.setattr(coordinator, "get_state", read_then_a_sibling_starts)
        try:
            assert await refused_delete(row, bulk=False) is not None
        finally:
            monkeypatch.setattr(coordinator, "get_state", real_get_state)

        assert sibling and sibling[0] is not None
        state = await coordinator.get_state(str(row.id))
        assert state.observed is LifecycleStatus.STARTING, state
        await coordinator.release(sibling[0])


async def test_the_first_retry_of_an_unreachable_node_comes_one_minute_after_the_delete(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        before = datetime.now(UTC)
        assert await refused_delete(row, bulk=False) is None
        after = datetime.now(UTC)
        h.db_nodes.pop(row.id)
        assert await stops_owed_to(row.address) == 1
        remote.lost = set()

        await drain_owed_stops(monkeypatch, before + timedelta(seconds=59))
        assert remote.running, "the owed Stop was retried before its first minute"

        await drain_owed_stops(monkeypatch, after + timedelta(seconds=61))
        assert not remote.running, "the owed Stop was not retried a minute after the delete"
        assert await stops_owed_to(row.address) == 0


async def test_shared_workers_recheck_a_confirmed_stop_only_after_the_whole_settle_window(monkeypatch):
    quiet_side_effects(monkeypatch)
    module = retirement()
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        monkeypatch.setattr(module, "needs_shared_bridge_memory", lambda: True)
        monkeypatch.setattr(module, "publish_node_sync", AsyncMock())
        before = datetime.now(UTC)
        assert await refused_delete(row, bulk=False) is None
        after = datetime.now(UTC)
        h.db_nodes.pop(row.id)

        remote.running = True
        await drain_owed_stops(monkeypatch, before + timedelta(seconds=299))
        assert remote.running, "the settle recheck ran before its five minutes"

        await drain_owed_stops(monkeypatch, after + timedelta(seconds=301))
        assert not remote.running
        assert await stops_owed_to(row.address) == 0


async def test_an_unanswered_owed_stop_backs_off_doubling_to_an_hour_and_keeps_its_latest_error(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)
        real_rpc = remote.rpc
        attempt_marks = []

        async def silent_with_a_new_error_each_time(name, request):
            if name == "GetBaseInfo":
                attempt_marks.append(f"silent-attempt-{len(attempt_marks) + 1}")
                raise NodeAPIError(-1, attempt_marks[-1])
            return await real_rpc(name, request)

        remote.rpc = silent_with_a_new_error_each_time
        moment = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=2)
        for attempt, wait in enumerate([60, 120, 240, 480, 960, 1920, 3600, 3600], start=1):
            await drain_owed_stops(monkeypatch, moment)
            ((attempts, next_attempt_at, last_error, _reason),) = await owed_rows(row.address)
            expected_next = moment + timedelta(seconds=wait)
            next_attempt_at = as_utc(datetime.fromisoformat(str(next_attempt_at)))
            assert attempts == attempt
            assert abs((next_attempt_at - expected_next).total_seconds()) < 1, (attempt, next_attempt_at)
            assert attempt_marks[-1] in (last_error or ""), (attempt_marks[-1], last_error)
            moment = expected_next
        assert remote.running


async def test_an_unanswered_owed_stop_is_kept_for_its_whole_retention(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)

        nearly_a_week = datetime.now(UTC) + timedelta(days=6, hours=23)
        await drain_owed_stops(monkeypatch, nearly_a_week)
        assert await stops_owed_to(row.address) == 1, "an owed Stop was given up before its seven days"

        remote.lost = set()
        await drain_owed_stops(monkeypatch, nearly_a_week + timedelta(minutes=2))
        assert not remote.running
        assert await stops_owed_to(row.address) == 0


async def test_an_owed_stop_is_retried_outside_the_shared_lifecycle_of_the_deleted_node(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)
        assert await stops_owed_to(row.address) == 1

        coordinator = get_default_lifecycle_coordinator()
        stale = await coordinator.try_acquire(str(row.id), "gone-worker", LifecycleOperation.START, 3600)
        assert stale is not None
        epoch = (await coordinator.get_state(str(row.id))).epoch
        remote.lost = set()
        try:
            await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(hours=1))
            assert not remote.running, "a stale shared lifecycle lease of the deleted node blocked its owed Stop"
            assert (await coordinator.get_state(str(row.id))).epoch == epoch
            assert await stops_owed_to(row.address) == 0
        finally:
            await coordinator.release(stale)


async def test_an_owed_stop_is_sent_with_the_connection_settings_of_the_deleted_node(monkeypatch):
    from app import node as app_node

    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        async with Session() as session:
            await session.execute(
                update(Node).where(Node.id == row.id).values(port=2, api_port=3, default_timeout=7, internal_timeout=9)
            )
            await session.commit()
        expected = {
            "address": row.address,
            "port": 2,
            "api_port": 3,
            "server_ca": row.server_ca,
            "api_key": row.api_key,
            "default_timeout": 7,
            "internal_timeout": 9,
            "node_id": str(row.id),
        }
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)
        assert await stops_owed_to(row.address) == 1

        built = []
        create = app_node.create_node

        def recording_create(**kwargs):
            built.append({key: kwargs.get(key) for key in expected})
            return create(**kwargs)

        monkeypatch.setattr(app_node, "create_node", recording_create)
        remote.lost = set()
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(hours=1))

        assert not remote.running
        assert built and all(kwargs == expected for kwargs in built), built


async def test_a_bulk_delete_deletes_the_nodes_that_stopped_notifies_only_them_and_names_the_kept_ones(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        kept_row, kept_node = await managed_row(h, monkeypatch)
        gone_row, gone_node = await managed_row(h, monkeypatch)
        h.remote(kept_node).lost = {"Stop"}

        operation = NodeOperation(operator_type=OperatorType.API)
        async with Session() as db:
            with pytest.raises(HTTPException) as refused:
                await operation.bulk_remove_nodes(db, BulkNodeSelection(ids={kept_row.id, gone_row.id}), ADMIN)
        await asyncio.sleep(0.1)
        h.db_nodes.pop(gone_row.id)

        assert refused.value.status_code == 503
        assert f'Deleted "{gone_row.name}"' in refused.value.detail, refused.value.detail
        assert f'"{kept_row.name}" is still running' in refused.value.detail, refused.value.detail
        assert await node_row_exists(kept_row.id) and not await node_row_exists(gone_row.id)
        assert h.remote(kept_node).running and not h.remote(gone_node).running
        notified = [call.args[0].id for call in node_operation.notification.remove_node.call_args_list]
        assert notified == [gone_row.id], notified


async def test_deleting_a_node_drops_the_user_updates_still_queued_for_it(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        h.remote(db_node).stream_fails = True
        await h.disable()
        store = get_default_user_sync_store()
        queued, claimed = await store.capture_queued(str(row.id))
        assert queued or claimed, "the failed push left nothing queued for the node"

        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(row.id)

        assert await store.capture_queued(str(row.id)) == ({}, 0)


async def test_a_stop_blocked_by_a_sibling_lifecycle_lease_refuses_the_delete_and_says_why(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        coordinator = get_default_lifecycle_coordinator()
        sibling = await coordinator.try_acquire(str(row.id), "sibling", LifecycleOperation.START, 60)
        assert sibling is not None
        try:
            refusal = await refused_delete(row, bulk=False, operator_type=OperatorType.API)
        finally:
            await coordinator.release(sibling)

        assert isinstance(refusal, HTTPException) and refusal.status_code == 503, refusal
        assert "still running" in refusal.detail and "Stop was not sent" in refusal.detail, refusal.detail
        assert remote.running and await node_row_exists(row.id)
        assert await node_manager.get_node(row.id) is not None

        assert await refused_delete(row, bulk=False) is None
        assert not remote.running and not await node_row_exists(row.id)
        h.db_nodes.pop(row.id)


async def test_a_node_that_answers_without_information_is_not_taken_as_stopped(monkeypatch):
    quiet_side_effects(monkeypatch)
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = {"Stop"}
        real_rpc = remote.rpc

        async def no_information(name, request):
            if name == "GetBaseInfo":
                return None
            return await real_rpc(name, request)

        remote.rpc = no_information
        refusal = await refused_delete(row, bulk=False)
        row_kept = await node_row_exists(row.id)
        if not row_kept:
            h.db_nodes.pop(row.id)

        assert remote.running
        assert row_kept or await stops_owed_to(row.address) == 1, (
            f"the core never confirmed it stopped, yet the row is gone with no Stop owed (refusal={refusal})"
        )


async def test_an_owed_stop_that_cannot_be_recorded_keeps_the_node_row(monkeypatch):
    quiet_side_effects(monkeypatch)
    module = retirement()
    async with Harness(monkeypatch) as h:
        row, db_node = await managed_row(h, monkeypatch)
        remote = h.remote(db_node)
        remote.lost = set(EVERY_RPC)
        real_owed_stop = module._owed_stop

        def unrecordable(*args, **kwargs):
            owed = real_owed_stop(*args, **kwargs)
            owed["name"] = None
            return owed

        monkeypatch.setattr(module, "_owed_stop", unrecordable)
        refusal = await refused_delete(row, bulk=False)

        assert refusal is not None
        assert await node_row_exists(row.id), "the row was deleted although its owed Stop was never recorded"
        assert await node_manager.get_node(row.id) is not None
        assert await stops_owed_to(row.address) == 0


async def test_a_bulk_delete_never_runs_two_row_deletes_at_once(monkeypatch):
    quiet_side_effects(monkeypatch)
    module = retirement()
    async with Harness(monkeypatch) as h:
        managed = [await managed_row(h, monkeypatch) for _ in range(3)]
        real_delete = module.delete_node_rows
        in_flight = [0]
        seen = []

        async def tracked_delete(*args, **kwargs):
            in_flight[0] += 1
            seen.append(in_flight[0])
            try:
                await asyncio.sleep(0.05)
                await real_delete(*args, **kwargs)
            finally:
                in_flight[0] -= 1

        monkeypatch.setattr(module, "delete_node_rows", tracked_delete)
        operation = NodeOperation(operator_type=OperatorType.SYSTEM)
        async with Session() as db:
            result = await operation.bulk_remove_nodes(db, BulkNodeSelection(ids={row.id for row, _ in managed}), ADMIN)
        for row, _ in managed:
            h.db_nodes.pop(row.id)

        assert result.count == 3
        assert seen and max(seen) == 1, seen


async def test_one_failing_owed_stop_does_not_abort_the_drain_of_the_others(monkeypatch):
    quiet_side_effects(monkeypatch)
    module = retirement()
    async with Harness(monkeypatch) as h:
        broken_row, broken_node = await managed_row(h, monkeypatch)
        row, db_node = await managed_row(h, monkeypatch)
        for node in (broken_node, db_node):
            h.remote(node).lost = set(EVERY_RPC)
        assert await refused_delete(broken_row, bulk=False) is None
        assert await refused_delete(row, bulk=False) is None
        h.db_nodes.pop(broken_row.id)
        h.db_nodes.pop(row.id)
        h.remote(db_node).lost = set()
        real_check = module._server_is_a_node_again

        async def broken_for_one(owed):
            if owed.address == broken_row.address:
                raise RuntimeError("database went away")
            return await real_check(owed)

        monkeypatch.setattr(module, "_server_is_a_node_again", broken_for_one)
        await drain_owed_stops(monkeypatch, datetime.now(UTC) + timedelta(hours=1))

        assert not h.remote(db_node).running
        assert await stops_owed_to(row.address) == 0
        assert await stops_owed_to(broken_row.address) == 1
