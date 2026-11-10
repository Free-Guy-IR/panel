import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, update

from app.db.crud.node import create_node as db_create_node, get_node_by_id, remove_node as db_remove_node
from app.db.models import CoreConfig, Node, NodeStatus
from app.fork.operation.node_extras import connect_snapshot, with_connect_snapshot
from app.models.node import NodeCreate
from app.node import node_manager
from app.operation import OperatorType
from app.operation.node import NodeOperation
from tests.api import TestSession
from tests.api.helpers import create_core, unique_name
from tests.api.test_bulk_delete_entities import delete_core_if_present, delete_db_node, node_create_payload
from tests.api.test_core_delete_restarts_its_nodes import make_node


@dataclass(frozen=True)
class Push:
    caller: str
    node_id: int
    core_id: int


class UnreachableBridge:
    async def get_health(self):
        raise ConnectionError("node did not answer the health probe")


@pytest.fixture
def gated_connects(monkeypatch):
    pushes: list[Push] = []
    held_callers: dict[str, tuple[asyncio.Event, asyncio.Event]] = {}
    current_caller: dict[int, str] = {}

    async def bridge(_manager, _node_id):
        return UnreachableBridge()

    async def register(node):
        caller = current_caller.get(id(node))
        gates = held_callers.get(caller)
        if gates is not None:
            snapshot_taken, release = gates
            snapshot_taken.set()
            await release.wait()
        return True

    async def fenced(
        pg_node,
        db_node,
        core,
        users,
        extra_cores,
        backend_type,
        force_start,
        stop_first,
        old_status,
        held,
        push_on_attach=True,
    ):
        pushes.append(Push(caller=current_caller.get(id(db_node), "?"), node_id=db_node.id, core_id=core.core_id))
        return {
            "node_id": db_node.id,
            "status": NodeStatus.connected,
            "message": "",
            "xray_version": "25.9.0",
            "node_version": "0.9.0",
            "old_status": old_status,
        }

    async def cores_by_id_placeholders(db, core_ids, *, lazy=False):
        return (
            {core_id: SimpleNamespace(type="xray", inbounds=[], protocols=[], core_id=core_id) for core_id in core_ids},
            {core_id: [] for core_id in core_ids},
        )

    loaded_node = get_node_by_id

    def tagging_loader(caller_of):
        async def load(db, node_id, **kwargs):
            db_node = await loaded_node(db, node_id, **kwargs)
            if db_node is not None:
                current_caller[id(db_node)] = caller_of(db)
            return db_node

        return load

    monkeypatch.setattr(type(node_manager), "get_node", bridge)
    monkeypatch.setattr(NodeOperation, "_get_core_users_map", staticmethod(cores_by_id_placeholders))
    monkeypatch.setattr(NodeOperation, "_register_if_wanted", staticmethod(register))
    monkeypatch.setattr(NodeOperation, "_connect_fenced", staticmethod(fenced))
    monkeypatch.setattr("app.operation.node.notification", AsyncMock())
    monkeypatch.setattr("app.operation.node.update_node_status", AsyncMock())
    monkeypatch.setattr("app.operation.node.bulk_update_node_status", AsyncMock())

    return SimpleNamespace(
        pushes=pushes,
        held_callers=held_callers,
        current_caller=current_caller,
        tagging_loader=tagging_loader,
        monkeypatch=monkeypatch,
    )


async def move_node_to_core(node_id: int, core_id: int) -> None:
    async with TestSession() as session:
        await session.execute(update(Node).where(Node.id == node_id).values(core_config_id=core_id))
        await session.commit()


async def delete_core_row(core_id: int) -> None:
    async with TestSession() as session:
        await session.execute(delete(CoreConfig).where(CoreConfig.id == core_id))
        await session.commit()


def assert_node_ends_on_current_core(pushes: list[Push], node_id: int, old_core: int, new_core: int) -> None:
    mine = [push for push in pushes if push.node_id == node_id]
    assert mine, pushes
    assert mine[-1].core_id == new_core, f"node {node_id} ends on deleted core {old_core}: {mine}"
    first_current = next(index for index, push in enumerate(mine) if push.core_id == new_core)
    assert not [push for push in mine[first_current:] if push.core_id == old_core], mine


@pytest.mark.parametrize("stale_force", [True, False], ids=["stale-forced", "stale-health-reconnect"])
def test_a_connect_that_read_the_node_before_an_edit_never_pushes_the_deleted_core_after_the_edit_restart(
    access_token, gated_connects, stale_force
):
    old_core = create_core(access_token, name=unique_name("stale_old"))
    new_core = create_core(access_token, name=unique_name("stale_new"))
    node = make_node(old_core["id"], node_status=NodeStatus.error)
    sessions: dict[int, str] = {}
    gated_connects.monkeypatch.setattr(
        "app.operation.node.get_node_by_id", gated_connects.tagging_loader(lambda db: sessions[id(db)])
    )

    async def scenario():
        snapshot_taken, release = asyncio.Event(), asyncio.Event()
        gated_connects.held_callers["C1"] = (snapshot_taken, release)
        operation = NodeOperation(operator_type=OperatorType.API)

        async with TestSession() as db1:
            sessions[id(db1)] = "C1"
            stale = asyncio.create_task(operation._connect_single_node_local(db1, node["id"], force_start=stale_force))
            await snapshot_taken.wait()

            await move_node_to_core(node["id"], new_core["id"])
            async with TestSession() as db2:
                sessions[id(db2)] = "C2"
                await operation._connect_single_node_local(db2, node["id"], force_start=True)
            await delete_core_row(old_core["id"])

            release.set()
            await stale

    try:
        asyncio.run(scenario())

        assert [push.caller for push in gated_connects.pushes if push.caller == "C2"] == ["C2"]
        assert_node_ends_on_current_core(gated_connects.pushes, node["id"], old_core["id"], new_core["id"])
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, old_core["id"])
        delete_core_if_present(access_token, new_core["id"])


def test_a_bulk_connect_that_read_the_node_before_an_edit_never_pushes_the_deleted_core_after_the_edit_restart(
    access_token, gated_connects
):
    old_core = create_core(access_token, name=unique_name("stale_bulk_old"))
    new_core = create_core(access_token, name=unique_name("stale_bulk_new"))
    node = make_node(old_core["id"], node_status=NodeStatus.error)
    sessions: dict[int, str] = {}
    gated_connects.monkeypatch.setattr(
        "app.operation.node.get_node_by_id", gated_connects.tagging_loader(lambda db: sessions[id(db)])
    )

    async def scenario():
        snapshot_taken, release = asyncio.Event(), asyncio.Event()
        gated_connects.held_callers["C1"] = (snapshot_taken, release)
        operation = NodeOperation(operator_type=OperatorType.API)

        async with TestSession() as db1:
            sessions[id(db1)] = "C1"
            bulk_node = await get_node_by_id(db1, node["id"], load_usage_logs=False)
            gated_connects.current_caller[id(bulk_node)] = "C1"
            stale = asyncio.create_task(operation._connect_nodes_bulk_local(db1, [bulk_node], force_start=True))
            await snapshot_taken.wait()

            await move_node_to_core(node["id"], new_core["id"])
            async with TestSession() as db2:
                sessions[id(db2)] = "C2"
                await operation._connect_single_node_local(db2, node["id"], force_start=True)
            await delete_core_row(old_core["id"])

            release.set()
            await stale

    try:
        asyncio.run(scenario())

        assert [push.caller for push in gated_connects.pushes if push.caller == "C2"] == ["C2"]
        assert_node_ends_on_current_core(gated_connects.pushes, node["id"], old_core["id"], new_core["id"])
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, old_core["id"])
        delete_core_if_present(access_token, new_core["id"])


def test_a_connect_that_read_the_node_after_the_edit_still_pushes_when_an_older_one_runs_first(
    access_token, gated_connects
):
    old_core = create_core(access_token, name=unique_name("stale_order_old"))
    new_core = create_core(access_token, name=unique_name("stale_order_new"))
    node = make_node(old_core["id"], node_status=NodeStatus.error)
    sessions: dict[int, str] = {}
    gated_connects.monkeypatch.setattr(
        "app.operation.node.get_node_by_id", gated_connects.tagging_loader(lambda db: sessions[id(db)])
    )

    async def scenario():
        operation = NodeOperation(operator_type=OperatorType.API)
        async with TestSession() as db1:
            sessions[id(db1)] = "C1"
            await operation._connect_single_node_local(db1, node["id"], force_start=True)
        await move_node_to_core(node["id"], new_core["id"])
        async with TestSession() as db2:
            sessions[id(db2)] = "C2"
            await operation._connect_single_node_local(db2, node["id"], force_start=True)
        async with TestSession() as db3:
            sessions[id(db3)] = "C3"
            await operation._connect_single_node_local(db3, node["id"], force_start=True)

    try:
        asyncio.run(scenario())

        assert [(push.caller, push.core_id) for push in gated_connects.pushes] == [
            ("C1", old_core["id"]),
            ("C2", new_core["id"]),
            ("C3", new_core["id"]),
        ]
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, old_core["id"])
        delete_core_if_present(access_token, new_core["id"])


@pytest.mark.parametrize("stale_force", [True, False], ids=["stale-forced", "stale-health-reconnect"])
def test_a_connect_whose_node_read_returns_before_an_edit_commits_never_pushes_the_old_core_after_the_edit_restart(
    access_token, gated_connects, stale_force
):
    old_core = create_core(access_token, name=unique_name("stale_read_old"))
    new_core = create_core(access_token, name=unique_name("stale_read_new"))
    node = make_node(old_core["id"], node_status=NodeStatus.error)
    sessions: dict[int, str] = {}
    read_returned, release_read = asyncio.Event(), asyncio.Event()

    async def read_then_hold_the_stale_caller(db, node_id, **kwargs):
        db_node = await get_node_by_id(db, node_id, **kwargs)
        caller = sessions[id(db)]
        if db_node is not None:
            gated_connects.current_caller[id(db_node)] = caller
        if caller == "C1":
            read_returned.set()
            await release_read.wait()
        return db_node

    gated_connects.monkeypatch.setattr("app.operation.node.get_node_by_id", read_then_hold_the_stale_caller)

    async def scenario():
        operation = NodeOperation(operator_type=OperatorType.API)

        async with TestSession() as db1:
            sessions[id(db1)] = "C1"
            stale = asyncio.create_task(operation._connect_single_node_local(db1, node["id"], force_start=stale_force))
            await read_returned.wait()

            await move_node_to_core(node["id"], new_core["id"])
            async with TestSession() as db2:
                sessions[id(db2)] = "C2"
                await operation._connect_single_node_local(db2, node["id"], force_start=True)
            await delete_core_row(old_core["id"])

            release_read.set()
            await stale

    try:
        asyncio.run(scenario())

        assert [push.caller for push in gated_connects.pushes if push.caller == "C2"] == ["C2"]
        assert_node_ends_on_current_core(gated_connects.pushes, node["id"], old_core["id"], new_core["id"])
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, old_core["id"])
        delete_core_if_present(access_token, new_core["id"])


@pytest.mark.parametrize(
    "start_failure", [TimeoutError("start reply lost"), asyncio.CancelledError()], ids=["start-timeout", "start-cancel"]
)
def test_a_newer_start_that_was_sent_but_failed_still_drops_a_stale_connect_that_runs_after_it(
    access_token, gated_connects, start_failure
):
    old_core = create_core(access_token, name=unique_name("stale_fail_old"))
    new_core = create_core(access_token, name=unique_name("stale_fail_new"))
    node = make_node(old_core["id"], node_status=NodeStatus.error)
    sessions: dict[int, str] = {}
    gated_connects.monkeypatch.setattr(
        "app.operation.node.get_node_by_id", gated_connects.tagging_loader(lambda db: sessions[id(db)])
    )
    push_reaching_the_node = NodeOperation._connect_fenced

    async def sent_then_failed_for_the_edit(pg_node, db_node, *args, **kwargs):
        result = await push_reaching_the_node(pg_node, db_node, *args, **kwargs)
        if gated_connects.current_caller.get(id(db_node)) == "C2":
            raise start_failure
        return result

    gated_connects.monkeypatch.setattr(NodeOperation, "_connect_fenced", staticmethod(sent_then_failed_for_the_edit))

    async def scenario():
        snapshot_taken, release = asyncio.Event(), asyncio.Event()
        gated_connects.held_callers["C1"] = (snapshot_taken, release)
        operation = NodeOperation(operator_type=OperatorType.API)

        async with TestSession() as db1:
            sessions[id(db1)] = "C1"
            stale = asyncio.create_task(operation._connect_single_node_local(db1, node["id"], force_start=True))
            await snapshot_taken.wait()

            await move_node_to_core(node["id"], new_core["id"])
            async with TestSession() as db2:
                sessions[id(db2)] = "C2"
                edit_restart = asyncio.create_task(
                    operation._connect_single_node_local(db2, node["id"], force_start=True)
                )
                with pytest.raises(type(start_failure)):
                    await edit_restart
            await delete_core_row(old_core["id"])

            release.set()
            await stale

    try:
        asyncio.run(scenario())

        assert [push.caller for push in gated_connects.pushes if push.caller == "C2"] == ["C2"]
        assert_node_ends_on_current_core(gated_connects.pushes, node["id"], old_core["id"], new_core["id"])
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, old_core["id"])
        delete_core_if_present(access_token, new_core["id"])


async def remove_node_like_the_api(operation: NodeOperation, node_id: int) -> None:
    await operation._remove_node_impl(node_id)
    async with TestSession() as session:
        await db_remove_node(session, await session.get(Node, node_id))


async def create_node_reusing_id(node_id: int, core_id: int) -> None:
    async with TestSession() as session:
        db_node = await db_create_node(session, NodeCreate(**node_create_payload(unique_name("reused_id"), core_id)))
        await session.execute(update(Node).where(Node.id == db_node.id).values(id=node_id, status=NodeStatus.error))
        await session.commit()


def test_a_removed_node_never_drops_the_first_connect_of_a_new_node_that_reuses_its_id(access_token, gated_connects):
    old_core = create_core(access_token, name=unique_name("reused_old"))
    new_core = create_core(access_token, name=unique_name("reused_new"))
    node = make_node(old_core["id"], node_status=NodeStatus.error)
    sessions: dict[int, str] = {}
    gated_connects.monkeypatch.setattr(
        "app.operation.node.get_node_by_id", gated_connects.tagging_loader(lambda db: sessions[id(db)])
    )

    async def scenario():
        operation = NodeOperation(operator_type=OperatorType.API)
        drawn_before_the_old_push = connect_snapshot()
        async with TestSession() as db1:
            sessions[id(db1)] = "C1"
            await operation._connect_single_node_local(db1, node["id"], force_start=True)

        await remove_node_like_the_api(operation, node["id"])
        await create_node_reusing_id(node["id"], new_core["id"])

        async with TestSession() as db2:
            reused = await get_node_by_id(db2, node["id"], load_usage_logs=False)
            gated_connects.current_caller[id(reused)] = "C2"
            core = SimpleNamespace(type="xray", inbounds=[], protocols=[], core_id=new_core["id"])
            await with_connect_snapshot(
                drawn_before_the_old_push, NodeOperation.connect_node(reused, core, [], None, force_start=True)
            )

    try:
        asyncio.run(scenario())

        assert [(push.caller, push.core_id) for push in gated_connects.pushes] == [
            ("C1", old_core["id"]),
            ("C2", new_core["id"]),
        ]
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, old_core["id"])
        delete_core_if_present(access_token, new_core["id"])
