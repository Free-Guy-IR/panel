import pytest
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.core import remove_core_config
from app.db.crud.node import create_node, remove_node
from app.db.models import CoreConfig, node_additional_cores_association
from app.models.node import NodeCreate

CA = '-----BEGIN CERTIFICATE-----\nMIIBcjCCARmgAwIBAgIUdV0vwcjxssCWTR7ZNbk3lEMvbQIwCgYIKoZIzj0EAwIw\nDzENMAsGA1UEAwwEdGVzdDAeFw0yNjA5MTAwMzE0MDNaFw0zNjA5MDcwMzE0MDNa\nMA8xDTALBgNVBAMMBHRlc3QwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATPtMRV\nIcsqle2WNhYVI3Qa4b/U7J0OvKTVX4gMMnGNecCwePx7ij1ruJZ5jTagGMWecP37\n39hu0vS5M8Zp+DB2o1MwUTAdBgNVHQ4EFgQUX8cfcsoCACw42NLMyee13uwqU6Ew\nHwYDVR0jBBgwFoAUX8cfcsoCACw42NLMyee13uwqU6EwDwYDVR0TAQH/BAUwAwEB\n/zAKBggqhkjOPQQDAgNHADBEAiBa4jx9eYImly1jqzt+toiiqnE77Y/YpTNjLjLo\n+DruVAIgA9dizrbaDAA7Y7/Hwe0MjGSetxelEiE63m2DpxMtkmo=\n-----END CERTIFICATE-----\n'


def payload(name, port, core_id, extras=None, key="00000000-0000-0000-0000-000000000000"):
    body = {
        "name": name,
        "address": "127.0.0.1",
        "port": port,
        "api_port": port + 1,
        "connection_type": "grpc",
        "server_ca": CA,
        "keep_alive": 0,
        "core_config_id": core_id,
        "api_key": key,
    }
    if extras is not None:
        body["additional_core_config_ids"] = extras
    return body


async def _first_core_ids(db, count):
    result = await db.execute(select(CoreConfig.id).order_by(CoreConfig.id).limit(count))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_a_node_with_no_extras_reports_none_exactly_as_before():
    async with GetDB() as db:
        cores = await _first_core_ids(db, 1)
        node = await create_node(db, NodeCreate(**payload("assoc-plain", 64500, cores[0])))
        try:
            assert node.additional_core_config_ids is None
            assert node.additional_cores == []
        finally:
            await remove_node(db, node)


@pytest.mark.asyncio
async def test_extras_round_trip_through_the_association_table():
    async with GetDB() as db:
        cores = await _first_core_ids(db, 3)
        node = await create_node(
            db, NodeCreate(**payload("assoc-extras", 64502, cores[0], [cores[1], cores[2]], "11111111-1111-1111-1111-111111111111"))
        )
        try:
            assert node.additional_core_config_ids == sorted([cores[1], cores[2]])
            rows = (
                await db.execute(
                    select(node_additional_cores_association.c.core_config_id).where(
                        node_additional_cores_association.c.node_id == node.id
                    )
                )
            ).scalars().all()
            assert sorted(rows) == sorted([cores[1], cores[2]])
        finally:
            await remove_node(db, node)


@pytest.mark.asyncio
async def test_deleting_a_node_leaves_no_association_rows_behind():
    async with GetDB() as db:
        cores = await _first_core_ids(db, 2)
        node = await create_node(
            db, NodeCreate(**payload("assoc-cleanup", 64504, cores[0], [cores[1]], "22222222-2222-2222-2222-222222222222"))
        )
        node_id = node.id
        await remove_node(db, node)

        left = (
            await db.execute(
                select(node_additional_cores_association.c.core_config_id).where(
                    node_additional_cores_association.c.node_id == node_id
                )
            )
        ).scalars().all()
        assert left == [], "a deleted node must not leave association rows that a reused id could inherit"


@pytest.mark.asyncio
async def test_a_reused_node_id_does_not_inherit_the_old_extras():
    async with GetDB() as db:
        cores = await _first_core_ids(db, 2)
        first = await create_node(
            db, NodeCreate(**payload("assoc-reuse-a", 64506, cores[0], [cores[1]], "33333333-3333-3333-3333-333333333333"))
        )
        await remove_node(db, first)

        second = await create_node(
            db, NodeCreate(**payload("assoc-reuse-b", 64508, cores[0], None, "44444444-4444-4444-4444-444444444444"))
        )
        try:
            assert second.additional_core_config_ids is None
        finally:
            await remove_node(db, second)


@pytest.mark.asyncio
async def test_deleting_a_core_leaves_no_association_rows_behind():
    async with GetDB() as db:
        cores = await _first_core_ids(db, 2)
        template = (await db.execute(select(CoreConfig).where(CoreConfig.id == cores[1]))).scalar_one()
        spare = CoreConfig(name="assoc-spare-core", type=template.type, config=template.config)
        db.add(spare)
        await db.commit()
        await db.refresh(spare)

        node = await create_node(
            db,
            NodeCreate(**payload("assoc-core-del", 64510, cores[0], [spare.id], "55555555-5555-5555-5555-555555555555")),
        )
        core_id = spare.id
        try:
            assert node.additional_core_config_ids == [core_id]
            await remove_core_config(db, spare)

            left = (
                await db.execute(
                    select(node_additional_cores_association.c.node_id).where(
                        node_additional_cores_association.c.core_config_id == core_id
                    )
                )
            ).scalars().all()
            assert left == [], "a deleted core must not leave rows that a reused core id could inherit"
        finally:
            await remove_node(db, node)
