import asyncio
import random
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import status
from PasarGuardNodeBridge import Health
from sqlalchemy import select, update

from app.db.crud.node import create_node as db_create_node
from app.db.models import CoreConfig, Node, NodeStatus, node_additional_cores_association
from app.models.node import NodeCreate
from app.node import node_manager
from app.operation.node import NodeOperation
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_core, unique_name
from tests.api.test_bulk_delete_entities import delete_core_if_present, delete_db_node, node_create_payload

EXTRAS = node_additional_cores_association


@dataclass(frozen=True)
class Start:
    node_id: int
    primary: int
    extras: tuple[int, ...]
    force: bool


class HealthyBridge:
    def __init__(self):
        self.fenced_starts = 0

    async def get_health(self):
        return Health.HEALTHY

    async def get_versions(self):
        return "0.9.0", "25.9.0"


@pytest.fixture
def starts(monkeypatch):
    issued: list[Start] = []

    async def healthy_bridge(_manager, _node_id):
        return HealthyBridge()

    async def register(_node):
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
        issued.append(
            Start(
                node_id=db_node.id,
                primary=db_node.core_config_id or 1,
                extras=tuple(sorted(extra[0] for extra in extra_cores or [])),
                force=force_start,
            )
        )
        return {
            "node_id": db_node.id,
            "status": NodeStatus.connected,
            "message": "",
            "xray_version": "25.9.0",
            "node_version": "0.9.0",
            "old_status": old_status,
        }

    loaded_cores = NodeOperation._get_core_users_map

    async def cores_with_a_loaded_default(db, core_ids, *, lazy=False):
        cores_by_id, users_by_core = await loaded_cores(db, core_ids, lazy=lazy)
        placeholder = SimpleNamespace(type="xray", inbounds=[], protocols=[])
        return {core_id: core or placeholder for core_id, core in cores_by_id.items()}, users_by_core

    monkeypatch.setattr(type(node_manager), "get_node", healthy_bridge)
    monkeypatch.setattr(NodeOperation, "_get_core_users_map", staticmethod(cores_with_a_loaded_default))
    monkeypatch.setattr(NodeOperation, "_register_if_wanted", staticmethod(register))
    monkeypatch.setattr(NodeOperation, "_connect_fenced", staticmethod(fenced))
    monkeypatch.setattr("app.operation.node.notification", AsyncMock())
    return issued


def create_singbox_core(access_token: str) -> dict:
    response = client.post(
        "/api/core",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("coredel_singbox"),
            "config": {
                "log": {"level": "error"},
                "inbounds": [
                    {
                        "type": "hysteria2",
                        "tag": unique_name("coredel_sb"),
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
            payload = node_create_payload(unique_name("coredel_node"), core_config_id)
            payload["additional_core_config_ids"] = additional
            db_node = await db_create_node(session, NodeCreate(**payload))
            await session.execute(update(Node).where(Node.id == db_node.id).values(status=node_status))
            await session.commit()
            return {"id": db_node.id, "name": db_node.name}

    node = asyncio.run(_make())
    node["refs"] = stored_node(node["id"])
    return node


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


def assert_no_node_left_on_a_deleted_core(response, affected: list[dict], target_ids: set[int], issued: list[Start]):
    context = f"status={response.status_code} body={response.text} issued={issued}"
    if response.status_code == status.HTTP_409_CONFLICT:
        for node in affected:
            assert stored_node(node["id"]) == node["refs"], context
        for core_id in target_ids:
            assert core_row_exists(core_id), context
        return
    assert response.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), context
    for core_id in target_ids:
        assert not core_row_exists(core_id), context
    for node in affected:
        forced = [start for start in issued if start.node_id == node["id"] and start.force]
        assert forced, f"node {node['id']} was left running deleted core(s) {sorted(target_ids)}; {context}"
        last = forced[-1]
        assert last.primary not in target_ids, context
        assert not set(last.extras) & target_ids, context


def assert_bystander_untouched(bystander: dict, issued: list[Start]):
    assert stored_node(bystander["id"]) == bystander["refs"]
    assert not [start for start in issued if start.node_id == bystander["id"] and start.force], issued


def delete_single(access_token: str, core_id: int, restart_nodes: bool = True):
    return client.delete(
        f"/api/core/{core_id}", headers=auth_headers(access_token), params={"restart_nodes": restart_nodes}
    )


def delete_bulk(access_token: str, core_ids: list[int]):
    return client.post("/api/cores/bulk/delete", headers=auth_headers(access_token), json={"ids": core_ids})


def test_a_non_forced_connect_leaves_a_healthy_connected_node_on_its_running_config(starts):
    node = SimpleNamespace(id=987654, name="coredel-probe", status=NodeStatus.connected, core_config_id=None)
    core = SimpleNamespace(type="xray", inbounds=[], protocols=[])

    assert asyncio.run(NodeOperation.connect_node(node, core, [], force_start=False)) is None
    assert starts == []

    asyncio.run(NodeOperation.connect_node(node, core, [], force_start=True))
    assert starts == [Start(node_id=987654, primary=1, extras=(), force=True)]


@pytest.mark.parametrize("node_status", [NodeStatus.connected, NodeStatus.error], ids=lambda value: value.value)
def test_single_delete_with_restart_never_leaves_an_active_node_on_the_deleted_primary_core(
    access_token, starts, node_status
):
    target = create_core(access_token, name=unique_name("coredel_target"))
    other = create_core(access_token, name=unique_name("coredel_other"))
    node = make_node(target["id"], node_status=node_status)
    bystander = make_node(other["id"])
    try:
        response = delete_single(access_token, target["id"])

        assert_no_node_left_on_a_deleted_core(response, [node], {target["id"]}, starts)
        assert_bystander_untouched(bystander, starts)
    finally:
        delete_db_node(node["id"])
        delete_db_node(bystander["id"])
        delete_core_if_present(access_token, target["id"])
        delete_core_if_present(access_token, other["id"])


@pytest.mark.parametrize("node_status", [NodeStatus.connected, NodeStatus.error], ids=lambda value: value.value)
def test_bulk_delete_never_leaves_an_active_node_on_a_deleted_primary_core(access_token, starts, node_status):
    target = create_core(access_token, name=unique_name("coredel_bulk_target"))
    other = create_core(access_token, name=unique_name("coredel_other"))
    node = make_node(target["id"], node_status=node_status)
    bystander = make_node(other["id"])
    try:
        response = delete_bulk(access_token, [target["id"]])

        assert_no_node_left_on_a_deleted_core(response, [node], {target["id"]}, starts)
        assert_bystander_untouched(bystander, starts)
    finally:
        delete_db_node(node["id"])
        delete_db_node(bystander["id"])
        delete_core_if_present(access_token, target["id"])
        delete_core_if_present(access_token, other["id"])


def test_bulk_delete_of_two_cores_never_leaves_either_node_on_a_deleted_core(access_token, starts):
    targets = [create_core(access_token, name=unique_name("coredel_bulk_pair")) for _ in range(2)]
    other = create_core(access_token, name=unique_name("coredel_other"))
    nodes = [make_node(core["id"]) for core in targets]
    bystander = make_node(other["id"])
    try:
        response = delete_bulk(access_token, [core["id"] for core in targets])

        assert_no_node_left_on_a_deleted_core(response, nodes, {core["id"] for core in targets}, starts)
        assert_bystander_untouched(bystander, starts)
    finally:
        for node in (*nodes, bystander):
            delete_db_node(node["id"])
        for core in (*targets, other):
            delete_core_if_present(access_token, core["id"])


@pytest.mark.parametrize("bulk", [False, True], ids=["single", "bulk"])
def test_a_core_no_node_uses_still_deletes_and_restarts_nothing(access_token, starts, bulk):
    target = create_core(access_token, name=unique_name("coredel_unused"))
    other = create_core(access_token, name=unique_name("coredel_other"))
    bystander = make_node(other["id"])
    try:
        response = delete_bulk(access_token, [target["id"]]) if bulk else delete_single(access_token, target["id"])

        assert response.status_code in (status.HTTP_200_OK, status.HTTP_204_NO_CONTENT), response.text
        assert not core_row_exists(target["id"])
        assert_bystander_untouched(bystander, starts)
        assert [start for start in starts if start.force] == []
    finally:
        delete_db_node(bystander["id"])
        delete_core_if_present(access_token, target["id"])
        delete_core_if_present(access_token, other["id"])
