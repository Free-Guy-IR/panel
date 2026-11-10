import asyncio
import random
import re
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, status
from sqlalchemy import delete, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.dml import UpdateBase
from sqlalchemy.sql.elements import ClauseElement, TextClause

from app.core.manager import core_manager
from app.db.base import Base
from app.db.crud.node import create_node as db_create_node
from app.db.models import CoreConfig, CoreType, Node, NodeStatus, node_additional_cores_association
from app.models.admin import AdminDetails, AdminRoleData
from app.models.core import BulkCoreSelection
from app.models.node import NodeCreate, NodeModify
from app.operation.core import CoreOperation
from app.operation.node import NodeOperation
from app.routers import core as core_router, node as node_router
from tests.api import DATABASE_URL, IS_SQLITE, TestSession, client
from tests.api.helpers import auth_headers, create_core, unique_name
from tests.api.test_bulk_delete_entities import delete_core_if_present, delete_db_node, node_create_payload

EXTRAS = node_additional_cores_association
OWNER = AdminDetails(id=None, username="core-delete-owner", role=AdminRoleData(id=1, name="owner", is_owner=True))


@pytest.fixture
def restarts(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(core_router.node_operator, "restart_nodes_by_ids", spy)
    monkeypatch.setattr(core_router.node_operator, "restart_all_node", spy)
    monkeypatch.setattr(core_router.node_operator, "connect_nodes_bulk", spy)
    return spy


def create_singbox_core(access_token: str) -> dict:
    response = client.post(
        "/api/core",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("cd_singbox"),
            "config": {
                "log": {"level": "error"},
                "inbounds": [
                    {
                        "type": "hysteria2",
                        "tag": unique_name("cd_sb"),
                        "listen": "127.0.0.1",
                        "listen_port": random.randint(20000, 60000),
                        "users": [],
                        "tls": {
                            "enabled": True,
                            "alpn": ["h3"],
                            "certificate_path": "/etc/ssl/certs/hy2.crt",
                            "key_path": "/etc/ssl/private/hy2.key",
                        },
                    }
                ],
                "outbounds": [{"type": "direct", "tag": "direct"}],
            },
            "type": "singbox",
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def make_node(core_config_id: int, *, additional: list[int] | None = None, node_status=NodeStatus.connected) -> dict:
    async def _make():
        async with TestSession() as session:
            payload = node_create_payload(unique_name("cd_node"), core_config_id)
            payload["additional_core_config_ids"] = additional
            db_node = await db_create_node(session, NodeCreate(**payload))
            await session.execute(update(Node).where(Node.id == db_node.id).values(status=node_status))
            await session.commit()
            return {"id": db_node.id, "name": db_node.name}

    return asyncio.run(_make())


def orphan_node(node_id: int) -> None:
    async def _orphan():
        async with TestSession() as session:
            await session.execute(update(Node).where(Node.id == node_id).values(core_config_id=None))
            await session.commit()

    asyncio.run(_orphan())


def stored_node(node_id: int) -> tuple[int | None, list[int]]:
    async def _read():
        async with TestSession() as session:
            primary = (await session.execute(select(Node.core_config_id).where(Node.id == node_id))).scalar_one()
            extras = (
                (await session.execute(select(EXTRAS.c.core_config_id).where(EXTRAS.c.node_id == node_id)))
                .scalars()
                .all()
            )
            return primary, sorted(extras)

    return asyncio.run(_read())


def core_row_exists(core_id: int) -> bool:
    async def _read():
        async with TestSession() as session:
            found = await session.execute(select(CoreConfig.id).where(CoreConfig.id == core_id))
            return found.scalar_one_or_none() is not None

    return asyncio.run(_read())


def core_is_loaded(core_id: int) -> bool:
    return core_id in asyncio.run(core_manager.get_cores({core_id}))


def assert_untouched(access_token: str, core: dict, loaded_before: bool) -> None:
    assert core_row_exists(core["id"])
    assert client.get(f"/api/core/{core['id']}", headers=auth_headers(access_token)).status_code == status.HTTP_200_OK
    assert core_is_loaded(core["id"]) == loaded_before


def assert_names(detail: str, node: dict, role: str) -> None:
    assert node["name"] in detail, detail
    assert re.search(rf"(?<!\d){node['id']}(?!\d)", detail), detail
    assert role in detail.lower(), detail


def assert_names_core(detail: str, core: dict) -> None:
    assert core["name"] in detail or re.search(rf"(?<!\d){core['id']}(?!\d)", detail), detail


@pytest.mark.parametrize(
    "node_status",
    [NodeStatus.connected, NodeStatus.disabled, NodeStatus.limited, NodeStatus.error],
    ids=lambda value: value.value,
)
def test_deleting_the_core_a_node_runs_on_is_refused_and_changes_nothing(access_token, restarts, node_status):
    core = create_core(access_token, name=unique_name("cd_primary"))
    node = make_node(core["id"], node_status=node_status)
    loaded_before = core_is_loaded(core["id"])
    try:
        response = client.delete(
            f"/api/core/{core['id']}", headers=auth_headers(access_token), params={"restart_nodes": True}
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        assert_names(detail, node, "primary")
        assert stored_node(node["id"]) == (core["id"], [])
        assert_untouched(access_token, core, loaded_before)
        restarts.assert_not_awaited()
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, core["id"])


def test_deleting_a_core_a_node_runs_as_an_additional_core_is_refused(access_token, restarts):
    extra = create_singbox_core(access_token)
    node = make_node(1, additional=[extra["id"]], node_status=NodeStatus.disabled)
    loaded_before = core_is_loaded(extra["id"])
    try:
        response = client.delete(
            f"/api/core/{extra['id']}", headers=auth_headers(access_token), params={"restart_nodes": True}
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        assert_names(detail, node, "additional")
        assert stored_node(node["id"]) == (1, [extra["id"]])
        node_view = client.get(f"/api/node/{node['id']}", headers=auth_headers(access_token))
        assert node_view.status_code == status.HTTP_200_OK
        assert node_view.json()["additional_core_config_ids"] == [extra["id"]]
        assert_untouched(access_token, extra, loaded_before)
        restarts.assert_not_awaited()
    finally:
        delete_db_node(node["id"])
        delete_core_if_present(access_token, extra["id"])


def test_bulk_delete_with_one_referenced_core_deletes_none_of_them(access_token, restarts):
    spare_before = create_core(access_token, name=unique_name("cd_bulk_spare"))
    used = create_core(access_token, name=unique_name("cd_bulk_used"))
    spare_after = create_core(access_token, name=unique_name("cd_bulk_spare"))
    node = make_node(used["id"])
    try:
        response = client.post(
            "/api/cores/bulk/delete",
            headers=auth_headers(access_token),
            json={"ids": [spare_before["id"], used["id"], spare_after["id"]]},
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        assert_names(detail, node, "primary")
        assert_names_core(detail, used)
        for core in (spare_before, used, spare_after):
            assert core_row_exists(core["id"]), core
        assert stored_node(node["id"]) == (used["id"], [])
        restarts.assert_not_awaited()
    finally:
        delete_db_node(node["id"])
        for core in (spare_before, used, spare_after):
            delete_core_if_present(access_token, core["id"])


def test_bulk_delete_names_every_blocking_core_and_node(access_token, restarts):
    primary_core = create_core(access_token, name=unique_name("cd_bulk_primary"))
    extra = create_singbox_core(access_token)
    spare = create_core(access_token, name=unique_name("cd_bulk_spare"))
    first = make_node(primary_core["id"], node_status=NodeStatus.disabled)
    second = make_node(1, additional=[extra["id"]], node_status=NodeStatus.limited)
    try:
        response = client.post(
            "/api/cores/bulk/delete",
            headers=auth_headers(access_token),
            json={"ids": [primary_core["id"], extra["id"], spare["id"]]},
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        detail = response.json()["detail"]
        assert isinstance(detail, str)
        assert_names(detail, first, "primary")
        assert_names(detail, second, "additional")
        assert_names_core(detail, primary_core)
        assert_names_core(detail, extra)
        for core in (primary_core, extra, spare):
            assert core_row_exists(core["id"]), core
        assert stored_node(first["id"]) == (primary_core["id"], [])
        assert stored_node(second["id"]) == (1, [extra["id"]])
        restarts.assert_not_awaited()
    finally:
        delete_db_node(first["id"])
        delete_db_node(second["id"])
        for core in (primary_core, extra, spare):
            delete_core_if_present(access_token, core["id"])


def test_a_core_no_node_uses_still_deletes(access_token, restarts):
    core = create_core(access_token, name=unique_name("cd_free"))
    other = create_core(access_token, name=unique_name("cd_other"))
    bystander = make_node(other["id"])
    try:
        response = client.delete(
            f"/api/core/{core['id']}", headers=auth_headers(access_token), params={"restart_nodes": True}
        )

        assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
        assert not core_row_exists(core["id"])
        assert client.get(f"/api/core/{core['id']}", headers=auth_headers(access_token)).status_code == (
            status.HTTP_404_NOT_FOUND
        )
        assert not core_is_loaded(core["id"])
        assert stored_node(bystander["id"]) == (other["id"], [])
        restarts.assert_not_awaited()
    finally:
        delete_db_node(bystander["id"])
        delete_core_if_present(access_token, core["id"])
        delete_core_if_present(access_token, other["id"])


def test_bulk_delete_of_cores_no_node_uses_still_deletes(access_token, restarts):
    cores = [create_core(access_token, name=unique_name("cd_bulk_free")) for _ in range(2)]
    other = create_core(access_token, name=unique_name("cd_other"))
    bystander = make_node(other["id"])
    try:
        response = client.post(
            "/api/cores/bulk/delete",
            headers=auth_headers(access_token),
            json={"ids": [core["id"] for core in cores]},
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["count"] == 2
        for core in cores:
            assert not core_row_exists(core["id"])
        assert stored_node(bystander["id"]) == (other["id"], [])
        restarts.assert_not_awaited()
    finally:
        delete_db_node(bystander["id"])
        for core in cores:
            delete_core_if_present(access_token, core["id"])
        delete_core_if_present(access_token, other["id"])


def test_a_node_already_left_without_a_core_neither_blocks_nor_changes(access_token, restarts):
    core = create_core(access_token, name=unique_name("cd_unrelated"))
    legacy = make_node(1, node_status=NodeStatus.disabled)
    orphan_node(legacy["id"])
    try:
        response = client.delete(f"/api/core/{core['id']}", headers=auth_headers(access_token))

        assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
        assert stored_node(legacy["id"]) == (None, [])
    finally:
        delete_db_node(legacy["id"])
        delete_core_if_present(access_token, core["id"])


def test_renaming_a_node_to_a_taken_name_is_a_conflict_not_a_crash(access_token, monkeypatch):
    monkeypatch.setattr(node_router.node_operator, "_update_node_impl", AsyncMock())
    monkeypatch.setattr(NodeOperation, "_connect_single_node_background", AsyncMock())
    monkeypatch.setattr(NodeOperation, "disconnect_single_node", AsyncMock())
    first = make_node(1, node_status=NodeStatus.disabled)
    second = make_node(1, node_status=NodeStatus.disabled)
    try:
        response = client.put(
            f"/api/node/{second['id']}", headers=auth_headers(access_token), json={"name": first["name"]}
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert first["name"] in response.json()["detail"]
        assert stored_node(second["id"]) == (1, [])
    finally:
        delete_db_node(first["id"])
        delete_db_node(second["id"])


def _foreign_keys_on(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _shape(method: str, target) -> tuple[str, bool]:
    writes = method in ("commit", "flush", "delete")
    if isinstance(target, UpdateBase) or getattr(target, "_for_update_arg", None) is not None:
        writes = True
    if isinstance(target, TextClause):
        text = target.text.strip().lower()
        writes = writes or text.split(" ", 1)[0] in ("insert", "update", "delete") or "for update" in text
    reads_core = target is CoreConfig
    if not reads_core and isinstance(target, ClauseElement) and not writes:
        reads_core = "core_configs" in str(target).lower()
    return ("write" if writes else "read"), reads_core


class Interleaver:
    def __init__(self, policy, stall: float = 0.5):
        self.policy = policy
        self.stall = stall
        self.parked: dict[str, tuple[str, bool]] = {}
        self.waiters: dict[str, asyncio.Future] = {}
        self.history: dict[str, list[tuple[str, bool]]] = {}
        self.moved = asyncio.Event()
        self.trace: list[str] = []

    async def gate(self, actor: str, shape: tuple[str, bool]) -> None:
        future = asyncio.get_running_loop().create_future()
        self.parked[actor] = shape
        self.waiters[actor] = future
        self.moved.set()
        await future

    def _release(self, actor: str) -> None:
        shape = self.parked.pop(actor)
        self.history.setdefault(actor, []).append(shape)
        self.trace.append(f"{actor}:{shape[0]}{'+core' if shape[1] else ''}")
        self.waiters.pop(actor).set_result(None)

    async def _actor(self, name: str, body):
        await self.gate(name, ("start", False))
        try:
            return "ok", await body()
        except HTTPException as error:
            return "http", error
        except Exception as error:
            return "crash", error

    async def run(self, bodies: dict) -> dict:
        tasks = {name: asyncio.create_task(self._actor(name, body)) for name, body in bodies.items()}
        for task in tasks.values():
            task.add_done_callback(lambda _task: self.moved.set())
        while True:
            self.moved.clear()
            live = {name for name, task in tasks.items() if not task.done()}
            if not live:
                break
            if live - set(self.parked):
                try:
                    await asyncio.wait_for(self.moved.wait(), self.stall)
                    continue
                except TimeoutError:
                    pass
            if not self.parked:
                continue
            choice = self.policy(self, live)
            if choice not in self.parked:
                choice = min(self.parked)
            self._release(choice)
        return {name: task.result() for name, task in tasks.items()}


class GatedSession(AsyncSession):
    async def _pass(self, method: str, target=None) -> None:
        gate = self.info.get("gate")
        if gate is not None:
            interleaver, actor = gate
            await interleaver.gate(actor, _shape(method, target))

    async def execute(self, statement, *args, **kwargs):
        await self._pass("execute", statement)
        return await super().execute(statement, *args, **kwargs)

    async def scalar(self, statement, *args, **kwargs):
        await self._pass("execute", statement)
        return await super().scalar(statement, *args, **kwargs)

    async def get(self, entity, *args, **kwargs):
        await self._pass("get", entity)
        return await super().get(entity, *args, **kwargs)

    async def delete(self, instance):
        await self._pass("delete", instance)
        return await super().delete(instance)

    async def flush(self, objects=None):
        await self._pass("flush")
        return await super().flush(objects)

    async def commit(self):
        await self._pass("commit")
        return await super().commit()

    async def rollback(self):
        await self._pass("rollback")
        return await super().rollback()

    async def refresh(self, instance, *args, **kwargs):
        await self._pass("refresh", instance)
        return await super().refresh(instance, *args, **kwargs)


def seeded(seed: int):
    rng = random.Random(seed)

    def choose(interleaver: Interleaver, live: set[str]):
        return rng.choice(sorted(interleaver.parked))

    return choose


def prefer(actor: str):
    def choose(interleaver: Interleaver, live: set[str]):
        return actor if actor in interleaver.parked else None

    return choose


def delete_waits_at_its_first_write():
    def choose(interleaver: Interleaver, live: set[str]):
        held = interleaver.parked.get("delete")
        if "modify" in live and (held is None or held[0] == "write"):
            return "modify"
        return "delete"

    return choose


def modify_waits_after_core_reads(count: int):
    def choose(interleaver: Interleaver, live: set[str]):
        core_reads = sum(1 for _shape_kind, reads_core in interleaver.history.get("modify", []) if reads_core)
        if "delete" in live and core_reads >= count:
            return "delete"
        return "modify"

    return choose


POLICIES = {
    **{f"seed-{seed}": (lambda seed=seed: seeded(seed)) for seed in range(6)},
    "modify-first": lambda: prefer("modify"),
    "delete-first": lambda: prefer("delete"),
    "delete-checks-then-modify-commits": delete_waits_at_its_first_write,
    **{
        f"modify-reads-core-{count}x-then-delete": (lambda count=count: modify_waits_after_core_reads(count))
        for count in (1, 2, 3)
    },
}


@pytest.fixture
def quiet_side_effects(monkeypatch):
    monkeypatch.setattr("app.operation.core.core_manager", AsyncMock())
    monkeypatch.setattr("app.operation.core.notification", AsyncMock())
    monkeypatch.setattr("app.operation.node.notification", AsyncMock())
    monkeypatch.setattr(CoreOperation, "_refresh_hosts_from_db", AsyncMock())
    monkeypatch.setattr(node_router.node_operator, "_update_node_impl", AsyncMock())
    monkeypatch.setattr(NodeOperation, "_connect_single_node_background", AsyncMock())
    monkeypatch.setattr(NodeOperation, "disconnect_single_node", AsyncMock())
    for name in ("restart_nodes_by_ids", "restart_all_node", "connect_nodes_bulk"):
        monkeypatch.setattr(core_router.node_operator, name, AsyncMock())


async def _race_engine(tmp_path):
    if not IS_SQLITE:
        return create_async_engine(DATABASE_URL, poolclass=NullPool)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'core_delete_race.db'}")
    event.listen(engine.sync_engine, "connect", _foreign_keys_on)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add(CoreConfig(name="race default", type=CoreType.xray, config={}))
        await session.commit()
    return engine


async def _seed(engine, target_type: CoreType) -> dict:
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        home = CoreConfig(name=unique_name("race_home"), type=CoreType.xray, config={})
        target = CoreConfig(name=unique_name("race_target"), type=target_type, config={})
        session.add_all([home, target])
        await session.commit()
        node = await db_create_node(session, NodeCreate(**node_create_payload(unique_name("race_node"), home.id)))
        await session.execute(update(Node).where(Node.id == node.id).values(status=NodeStatus.connected))
        await session.commit()
        return {"home": home.id, "target": target.id, "node": node.id}


async def _final(engine, seeded_rows: dict) -> tuple[int | None, list[int], set[int]]:
    async with async_sessionmaker(engine)() as session:
        primary = (
            await session.execute(select(Node.core_config_id).where(Node.id == seeded_rows["node"]))
        ).scalar_one()
        extras = sorted(
            (await session.execute(select(EXTRAS.c.core_config_id).where(EXTRAS.c.node_id == seeded_rows["node"])))
            .scalars()
            .all()
        )
        wanted = {seeded_rows["home"], seeded_rows["target"], *extras, *([primary] if primary is not None else [])}
        alive = set((await session.execute(select(CoreConfig.id).where(CoreConfig.id.in_(wanted)))).scalars().all())
        return primary, extras, alive


async def _cleanup(engine, seeded_rows: dict) -> None:
    async with engine.begin() as connection:
        await connection.execute(delete(EXTRAS).where(EXTRAS.c.node_id == seeded_rows["node"]))
        await connection.execute(delete(Node.__table__).where(Node.id == seeded_rows["node"]))
        await connection.execute(
            delete(CoreConfig.__table__).where(CoreConfig.id.in_([seeded_rows["home"], seeded_rows["target"]]))
        )


async def _race(engine, policy, delete_body, modify_body) -> tuple[dict, list[str]]:
    sessions = async_sessionmaker(engine, class_=GatedSession, expire_on_commit=False, autoflush=False)
    interleaver = Interleaver(policy)
    async with sessions() as deleting, sessions() as modifying:
        deleting.info["gate"] = (interleaver, "delete")
        modifying.info["gate"] = (interleaver, "modify")
        outcomes = await interleaver.run(
            {"delete": lambda: delete_body(deleting), "modify": lambda: modify_body(modifying)}
        )
    return outcomes, interleaver.trace


def _deleter(bulk: bool, core_id: int):
    async def single(session):
        return await core_router.delete_core_config(core_id=core_id, restart_nodes=True, admin=OWNER, db=session)

    async def many(session):
        return await core_router.bulk_delete_cores(bulk_cores=BulkCoreSelection(ids={core_id}), db=session, admin=OWNER)

    return many if bulk else single


def _primary_mover(node_id: int, core_id: int):
    async def move(session):
        return await node_router.modify_node(
            modified_node=NodeModify(core_config_id=core_id), node_id=node_id, db=session, admin=OWNER
        )

    return move


def _extra_adder(node_id: int, core_id: int):
    async def add(session):
        return await node_router.modify_node(
            modified_node=NodeModify(additional_core_config_ids=[core_id]), node_id=node_id, db=session, admin=OWNER
        )

    return add


def _status(outcome) -> int | None:
    return outcome[1].status_code if outcome[0] == "http" else None


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
@pytest.mark.parametrize("policy", sorted(POLICIES))
async def test_a_core_delete_racing_a_node_moving_onto_it_never_strands_the_node(
    tmp_path, quiet_side_effects, policy, bulk
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
        context = f"policy={policy} trace={trace} delete={deleted} modify={moved} primary={primary} alive={alive}"

        assert deleted[0] != "crash" and moved[0] != "crash", context
        assert primary is not None, context
        assert primary in alive, context
        assert extras == [], context
        if deleted[0] == "ok":
            assert rows["target"] not in alive, context
            assert moved[0] == "http", context
            assert primary == rows["home"], context
        else:
            assert _status(deleted) == status.HTTP_409_CONFLICT, context
            assert rows["target"] in alive, context
        if moved[0] == "ok":
            assert primary == rows["target"], context
        else:
            assert primary == rows["home"], context
    finally:
        await _cleanup(engine, rows)
        await engine.dispose()


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
@pytest.mark.parametrize("policy", sorted(POLICIES))
async def test_a_core_delete_racing_a_node_adding_it_as_an_extra_never_drops_it_silently(
    tmp_path, quiet_side_effects, policy, bulk
):
    engine = await _race_engine(tmp_path)
    rows = await _seed(engine, CoreType.singbox)
    try:
        outcomes, trace = await _race(
            engine,
            POLICIES[policy](),
            _deleter(bulk, rows["target"]),
            _extra_adder(rows["node"], rows["target"]),
        )
        primary, extras, alive = await _final(engine, rows)
        deleted, added = outcomes["delete"], outcomes["modify"]
        context = f"policy={policy} trace={trace} delete={deleted} modify={added} extras={extras} alive={alive}"

        assert deleted[0] != "crash" and added[0] != "crash", context
        assert primary == rows["home"], context
        assert set(extras) <= alive, context
        if deleted[0] == "ok":
            assert rows["target"] not in alive, context
            assert added[0] == "http", context
        else:
            assert _status(deleted) == status.HTTP_409_CONFLICT, context
        if added[0] == "ok":
            assert extras == [rows["target"]], context
            assert added[1].additional_core_config_ids == [rows["target"]], context
        else:
            assert extras == [], context
    finally:
        await _cleanup(engine, rows)
        await engine.dispose()


async def test_a_node_modify_that_loses_its_core_to_a_delete_does_not_blame_the_node_name(tmp_path, quiet_side_effects):
    engine = await _race_engine(tmp_path)
    rows = await _seed(engine, CoreType.xray)
    try:
        outcomes, trace = await _race(
            engine,
            modify_waits_after_core_reads(1),
            _deleter(False, rows["target"]),
            _primary_mover(rows["node"], rows["target"]),
        )
        deleted, moved = outcomes["delete"], outcomes["modify"]
        context = f"trace={trace} delete={deleted} modify={moved}"

        assert deleted[0] == "ok", context
        assert moved[0] == "http", context
        assert "already exists" not in str(moved[1].detail), context
        assert moved[1].status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_409_CONFLICT), context
    finally:
        await _cleanup(engine, rows)
        await engine.dispose()
