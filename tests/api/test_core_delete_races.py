from unittest.mock import AsyncMock

import pytest
from fastapi import status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import CoreType, Node, NodeStatus
from app.models.node import NodeCreate
from app.operation.core import CoreOperation
from app.operation.node import NodeOperation
from app.routers import node as node_router
from tests.api.helpers import create_core, unique_name
from tests.api.test_bulk_delete_entities import delete_core_if_present, delete_db_node, node_create_payload
from tests.api.test_core_delete_keeps_nodes import (
    EXTRAS,
    OWNER,
    POLICIES,
    _cleanup,
    _deleter,
    _extra_adder,
    _final,
    _primary_mover,
    _race,
    _race_engine,
    _seed,
    _status,
)
from tests.api.test_core_delete_restarts_its_nodes import (
    assert_no_node_left_on_a_deleted_core,
    create_mtproto_core,
    delete_bulk,
    delete_single,
    make_node,
    starts,
    stored_node,
)

__all__ = ["starts"]


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
def test_a_delete_never_leaves_an_active_node_on_a_deleted_additional_core(access_token, starts, bulk):
    extra = create_mtproto_core(access_token)
    node = make_node(1, additional=[extra["id"]])
    try:
        response = delete_bulk(access_token, [extra["id"]]) if bulk else delete_single(access_token, extra["id"])

        assert_no_node_left_on_a_deleted_core(response, [node], {extra["id"]}, starts)
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, extra["id"])


def test_a_delete_without_restart_never_leaves_an_active_node_on_the_deleted_core(access_token, starts):
    target = create_core(access_token, name=unique_name("coredel_norestart"))
    node = make_node(target["id"])
    try:
        response = delete_single(access_token, target["id"], restart_nodes=False)

        assert_no_node_left_on_a_deleted_core(response, [node], {target["id"]}, starts)
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, target["id"])


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
@pytest.mark.parametrize("node_status", [NodeStatus.disabled, NodeStatus.limited], ids=lambda value: value.value)
def test_a_delete_never_moves_a_stopped_node_onto_the_default_core_behind_the_admins_back(
    access_token, starts, node_status, bulk
):
    target = create_core(access_token, name=unique_name("coredel_stopped"))
    node = make_node(target["id"], node_status=node_status)
    try:
        response = delete_bulk(access_token, [target["id"]]) if bulk else delete_single(access_token, target["id"])

        assert stored_node(node["id"]) == (target["id"], []), response.text
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, target["id"])


@pytest.fixture
def race_restarts(monkeypatch):
    issued: list[tuple[str, tuple[int, ...], bool]] = []

    async def connect_nodes_bulk(self, db, nodes, *, force_start=False):
        issued.append(("connect_nodes_bulk", tuple(sorted(node.id for node in nodes)), force_start))

    async def restart_nodes_by_ids(self, db, node_ids, admin):
        issued.append(("restart_nodes_by_ids", tuple(sorted(node_ids)), False))

    async def restart_all_node(self, db, admin, core_id=None):
        issued.append(("restart_all_node", (), False))

    monkeypatch.setattr("app.operation.core.core_manager", AsyncMock())
    monkeypatch.setattr("app.operation.core.notification", AsyncMock())
    monkeypatch.setattr("app.operation.node.notification", AsyncMock())
    monkeypatch.setattr(CoreOperation, "_refresh_hosts_from_db", AsyncMock())
    monkeypatch.setattr(node_router.node_operator, "_update_node_impl", AsyncMock())
    monkeypatch.setattr(NodeOperation, "_connect_single_node_background", AsyncMock())
    monkeypatch.setattr(NodeOperation, "disconnect_single_node", AsyncMock())
    monkeypatch.setattr(NodeOperation, "connect_nodes_bulk", connect_nodes_bulk)
    monkeypatch.setattr(NodeOperation, "restart_nodes_by_ids", restart_nodes_by_ids)
    monkeypatch.setattr(NodeOperation, "restart_all_node", restart_all_node)
    return issued


def force_restarted(issued, node_id: int) -> bool:
    return any(force and node_id in node_ids for _name, node_ids, force in issued)


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
@pytest.mark.parametrize("policy", sorted(POLICIES))
async def test_a_delete_racing_a_node_moving_onto_the_core_never_leaves_it_running_the_deleted_core(
    tmp_path, race_restarts, policy, bulk
):
    engine = await _race_engine(tmp_path)
    rows = await _seed(engine, CoreType.xray)
    try:
        outcomes, trace = await _race(
            engine,
            POLICIES[policy](),
            _deleter(bulk, rows["target"]),
            _primary_mover(rows["node"], rows["target"]),
        )
        primary, extras, alive = await _final(engine, rows)
        deleted, moved = outcomes["delete"], outcomes["modify"]
        context = (
            f"policy={policy} trace={trace} delete={deleted} modify={moved} primary={primary} alive={alive} "
            f"restarts={race_restarts}"
        )

        assert deleted[0] != "crash" and moved[0] != "crash", context
        assert primary is None or primary in alive, context
        assert extras == [], context
        if deleted[0] == "ok":
            assert rows["target"] not in alive, context
            if moved[0] == "ok":
                assert force_restarted(race_restarts, rows["node"]), context
        else:
            assert _status(deleted) == status.HTTP_409_CONFLICT, context
            assert rows["target"] in alive, context
            assert primary == (rows["target"] if moved[0] == "ok" else rows["home"]), context
        if moved[0] != "ok":
            assert primary == rows["home"], context
    finally:
        await _cleanup(engine, rows)
        await engine.dispose()


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
@pytest.mark.parametrize("policy", sorted(POLICIES))
async def test_a_delete_racing_a_node_adding_the_core_as_an_extra_never_leaves_it_running_nor_drops_it_silently(
    tmp_path, race_restarts, policy, bulk
):
    engine = await _race_engine(tmp_path)
    rows = await _seed(engine, CoreType.mtproto)
    try:
        outcomes, trace = await _race(
            engine,
            POLICIES[policy](),
            _deleter(bulk, rows["target"]),
            _extra_adder(rows["node"], rows["target"]),
        )
        primary, extras, alive = await _final(engine, rows)
        deleted, added = outcomes["delete"], outcomes["modify"]
        context = (
            f"policy={policy} trace={trace} delete={deleted} modify={added} extras={extras} alive={alive} "
            f"restarts={race_restarts}"
        )

        assert deleted[0] != "crash" and added[0] != "crash", context
        assert primary == rows["home"], context
        assert set(extras) <= alive, context
        if added[0] == "ok":
            assert added[1].additional_core_config_ids == [rows["target"]], context
        else:
            assert extras == [], context
        if deleted[0] == "ok":
            assert rows["target"] not in alive, context
            if added[0] == "ok":
                assert force_restarted(race_restarts, rows["node"]), context
        else:
            assert _status(deleted) == status.HTTP_409_CONFLICT, context
            assert extras == ([rows["target"]] if added[0] == "ok" else []), context
    finally:
        await _cleanup(engine, rows)
        await engine.dispose()


def _creator(name: str, core_id: int):
    async def create(session):
        return await node_router.create_node(
            new_node=NodeCreate(**node_create_payload(name, core_id)), db=session, admin=OWNER
        )

    return create


async def _node_by_name(engine, name: str) -> tuple[int, int | None] | None:
    async with async_sessionmaker(engine)() as session:
        row = (await session.execute(select(Node.id, Node.core_config_id).where(Node.name == name))).first()
        return None if row is None else (row[0], row[1])


async def _drop_node_by_name(engine, name: str) -> None:
    async with engine.begin() as connection:
        ids = (await connection.execute(select(Node.id).where(Node.name == name))).scalars().all()
        if ids:
            await connection.execute(delete(EXTRAS).where(EXTRAS.c.node_id.in_(ids)))
            await connection.execute(delete(Node.__table__).where(Node.id.in_(ids)))


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
@pytest.mark.parametrize("policy", sorted(POLICIES))
async def test_a_delete_racing_a_node_created_on_the_core_never_leaves_it_running_the_deleted_core(
    tmp_path, race_restarts, policy, bulk
):
    engine = await _race_engine(tmp_path)
    rows = await _seed(engine, CoreType.xray)
    name = unique_name("race_created")
    try:
        outcomes, trace = await _race(
            engine,
            POLICIES[policy](),
            _deleter(bulk, rows["target"]),
            _creator(name, rows["target"]),
        )
        deleted, created = outcomes["delete"], outcomes["modify"]
        created_row = await _node_by_name(engine, name)
        _primary, _extras, alive = await _final(engine, rows)
        context = (
            f"policy={policy} trace={trace} delete={deleted} create={created} row={created_row} alive={alive} "
            f"restarts={race_restarts}"
        )

        assert deleted[0] != "crash" and created[0] != "crash", context
        if created[0] == "ok":
            assert created_row is not None, context
        else:
            assert created_row is None, context
            assert "already exists" not in str(created[1].detail), context
        if deleted[0] == "ok":
            assert rows["target"] not in alive, context
            if created[0] == "ok":
                assert force_restarted(race_restarts, created_row[0]), context
        else:
            assert _status(deleted) == status.HTTP_409_CONFLICT, context
            assert rows["target"] in alive, context
            if created[0] == "ok":
                assert created_row[1] == rows["target"], context
    finally:
        await _drop_node_by_name(engine, name)
        await _cleanup(engine, rows)
        await engine.dispose()


def _restarts_reaching(issued, node_id: int) -> list:
    return [entry for entry in issued if entry[0] == "restart_all_node" or node_id in entry[1]]


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
@pytest.mark.parametrize("policy", sorted(POLICIES))
async def test_a_delete_racing_a_node_adding_the_core_as_an_extra_never_restarts_that_nodes_primary(
    tmp_path, race_restarts, policy, bulk
):
    engine = await _race_engine(tmp_path)
    rows = await _seed(engine, CoreType.mtproto)
    try:
        outcomes, trace = await _race(
            engine,
            POLICIES[policy](),
            _deleter(bulk, rows["target"]),
            _extra_adder(rows["node"], rows["target"]),
        )
        primary, extras, alive = await _final(engine, rows)
        deleted, added = outcomes["delete"], outcomes["modify"]
        context = (
            f"policy={policy} trace={trace} delete={deleted} modify={added} extras={extras} alive={alive} "
            f"restarts={race_restarts}"
        )

        assert deleted[0] != "crash" and added[0] != "crash", context
        assert primary == rows["home"], context
        assert not (deleted[0] == "ok" and added[0] == "ok"), context
        assert _restarts_reaching(race_restarts, rows["node"]) == [], context
    finally:
        await _cleanup(engine, rows)
        await engine.dispose()


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
@pytest.mark.parametrize("policy", sorted(POLICIES))
async def test_a_delete_racing_a_node_moving_off_the_core_only_restarts_it_onto_its_new_core(
    tmp_path, race_restarts, policy, bulk
):
    engine = await _race_engine(tmp_path)
    rows = await _seed(engine, CoreType.xray)
    async with engine.begin() as connection:
        await connection.execute(update(Node).where(Node.id == rows["node"]).values(core_config_id=rows["target"]))
    try:
        outcomes, trace = await _race(
            engine,
            POLICIES[policy](),
            _deleter(bulk, rows["target"]),
            _primary_mover(rows["node"], rows["home"]),
        )
        primary, extras, alive = await _final(engine, rows)
        deleted, moved = outcomes["delete"], outcomes["modify"]
        context = (
            f"policy={policy} trace={trace} delete={deleted} modify={moved} primary={primary} alive={alive} "
            f"restarts={race_restarts}"
        )

        assert deleted[0] != "crash" and moved[0] != "crash", context
        assert moved[0] == "ok", context
        assert primary == rows["home"] and primary in alive, context
        assert extras == [], context
        if deleted[0] == "ok":
            assert rows["target"] not in alive, context
            assert all(
                name == "connect_nodes_bulk" and force
                for name, _node_ids, force in _restarts_reaching(race_restarts, rows["node"])
            ), context
        else:
            assert _status(deleted) == status.HTTP_409_CONFLICT, context
            assert rows["target"] in alive, context
            assert _restarts_reaching(race_restarts, rows["node"]) == [], context
    finally:
        await _cleanup(engine, rows)
        await engine.dispose()
