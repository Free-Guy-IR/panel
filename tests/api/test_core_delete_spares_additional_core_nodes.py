import asyncio
import random

import pytest
from fastapi import status
from sqlalchemy import select, update

from app.core.manager import core_manager
from app.core.xray import XRayConfig
from app.db.crud.node import create_node as db_create_node
from app.db.models import CoreConfig, Node, NodeStatus, node_additional_cores_association
from app.models.node import NodeCreate
from app.node import node_manager
from app.operation import OperatorType
from app.operation.node import NodeOperation
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, unique_name
from tests.api.sample_data import XRAY_CONFIG
from tests.api.test_bulk_delete_entities import delete_core_if_present, delete_db_node, node_create_payload

EXTRAS = node_additional_cores_association


class RecordingBridge:
    full_sync_fence = None

    def __init__(self, calls: list[str]):
        self._calls = calls

    def __getattr__(self, name: str):
        async def record(*_args, **_kwargs):
            self._calls.append(name)

        return record


@pytest.fixture
def bridge_calls(monkeypatch):
    calls: list[str] = []
    bridge = RecordingBridge(calls)

    async def get_node(_manager, _node_id):
        return bridge

    async def update_node(_manager, _node):
        calls.append("manager.update_node")
        return bridge

    async def remove_node(_manager, _node_id, *, remote_stop=True):
        calls.append("manager.remove_node")

    if 1 not in core_manager._cores:
        monkeypatch.setitem(core_manager._cores, 1, XRayConfig(XRAY_CONFIG))
    monkeypatch.setattr(type(node_manager), "get_node", get_node)
    monkeypatch.setattr(type(node_manager), "update_node", update_node)
    monkeypatch.setattr(type(node_manager), "remove_node", remove_node)
    return calls


def create_singbox_core(access_token: str) -> dict:
    response = client.post(
        "/api/core",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("spare_singbox"),
            "config": {
                "log": {"level": "error"},
                "inbounds": [
                    {
                        "type": "hysteria2",
                        "tag": unique_name("spare_sb"),
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


def make_connected_node_with_extra(extra_core_id: int) -> int:
    async def _make():
        async with TestSession() as session:
            await NodeOperation(operator_type=OperatorType.API)._validate_additional_cores(session, [extra_core_id], 1)
            payload = node_create_payload(unique_name("spare_node"), 1)
            payload["additional_core_config_ids"] = [extra_core_id]
            db_node = await db_create_node(session, NodeCreate(**payload))
            await session.execute(update(Node).where(Node.id == db_node.id).values(status=NodeStatus.connected))
            await session.commit()
            return db_node.id

    return asyncio.run(_make())


def stored_links(node_id: int) -> tuple[int | None, list[int]]:
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


def delete_extra(access_token: str, core_id: int, how: str):
    if how == "bulk":
        return client.post("/api/cores/bulk/delete", headers=auth_headers(access_token), json={"ids": [core_id]})
    return client.delete(
        f"/api/core/{core_id}", headers=auth_headers(access_token), params={"restart_nodes": how == "restart"}
    )


@pytest.mark.parametrize("how", ["restart", "no_restart", "bulk"])
def test_deleting_a_core_a_node_runs_only_as_an_extra_never_touches_its_primary_backend(
    access_token, bridge_calls, how
):
    extra = create_singbox_core(access_token)
    node_id = make_connected_node_with_extra(extra["id"])
    try:
        response = delete_extra(access_token, extra["id"], how)

        assert (response.status_code, bridge_calls) == (status.HTTP_409_CONFLICT, []), response.text
        assert core_row_exists(extra["id"])
        assert stored_links(node_id) == (1, [extra["id"]])
        node_view = client.get(f"/api/node/{node_id}", headers=auth_headers(access_token))
        assert node_view.status_code == status.HTTP_200_OK
        assert node_view.json()["additional_core_config_ids"] == [extra["id"]]
        assert bridge_calls == []
    finally:
        delete_db_node(node_id)
        delete_core_if_present(access_token, extra["id"])
