import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.crud.core import remove_core_config
from app.db.crud.node import create_node, remove_node
from app.db.models import CoreConfig, node_additional_cores_association
from app.models.node import NodeCreate

CA = '-----BEGIN CERTIFICATE-----\nMIIBczCCARmgAwIBAgIUVQKhFxKK0mr0+1QS3LrGDVoalK8wCgYIKoZIzj0EAwIw\nDzENMAsGA1UEAwwEdGVzdDAeFw0yNjA5MTAwMzUwMDhaFw0zNjA5MDcwMzUwMDha\nMA8xDTALBgNVBAMMBHRlc3QwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATxcbWY\nKDBgpzLFh9bSpUmFczsT2DzZkAeW3x96HXBhuyOw+5rGfhG46mCb5uDnBwtCBXA3\nexSJSMIn8uMd8WQyo1MwUTAdBgNVHQ4EFgQUgUzvmjw781SOP+MSKiih74gBCtQw\nHwYDVR0jBBgwFoAUgUzvmjw781SOP+MSKiih74gBCtQwDwYDVR0TAQH/BAUwAwEB\n/zAKBggqhkjOPQQDAgNIADBFAiAuKDmRHv6Jr8mJO65oGfp9QHjddIMuUMwC7ONO\nbZw1hwIhAJ/yA/ovwkpkK9Uq19rHXbRcqx4Ka2aHGk5Yguh5LIR0\n-----END CERTIFICATE-----\n'


@pytest_asyncio.fixture
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'assoc.sqlite3'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def cores(db):
    made = []
    for name, core_type in (("h-xray", "xray"), ("h-singbox", "singbox"), ("h-openvpn", "openvpn")):
        core = CoreConfig(name=name, type=core_type, config={})
        db.add(core)
        made.append(core)
    await db.commit()
    for core in made:
        await db.refresh(core)
    return made


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


async def _rows_for_node(db, node_id):
    result = await db.execute(
        select(node_additional_cores_association.c.core_config_id).where(
            node_additional_cores_association.c.node_id == node_id
        )
    )
    return sorted(result.scalars().all())


@pytest.mark.asyncio
async def test_a_node_with_no_extras_reports_none_exactly_as_before(db, cores):
    node = await create_node(db, NodeCreate(**payload("h-plain", 64500, cores[0].id)))
    assert node.additional_core_config_ids is None
    assert node.additional_cores == []


@pytest.mark.asyncio
async def test_extras_round_trip_through_the_association_table(db, cores):
    node = await create_node(
        db, NodeCreate(**payload("h-extras", 64502, cores[0].id, [cores[1].id, cores[2].id], "11111111-1111-1111-1111-111111111111"))
    )
    assert node.additional_core_config_ids == sorted([cores[1].id, cores[2].id])
    assert await _rows_for_node(db, node.id) == sorted([cores[1].id, cores[2].id])


@pytest.mark.asyncio
async def test_deleting_a_node_leaves_no_association_rows_behind(db, cores):
    node = await create_node(
        db, NodeCreate(**payload("h-cleanup", 64504, cores[0].id, [cores[1].id], "22222222-2222-2222-2222-222222222222"))
    )
    node_id = node.id
    await remove_node(db, node)

    assert await _rows_for_node(db, node_id) == [], (
        "a deleted node must not leave rows that a reused id could inherit"
    )


@pytest.mark.asyncio
async def test_deleting_a_core_leaves_no_association_rows_behind(db, cores):
    node = await create_node(
        db, NodeCreate(**payload("h-core-del", 64506, cores[0].id, [cores[1].id], "33333333-3333-3333-3333-333333333333"))
    )
    core_id = cores[1].id
    await remove_core_config(db, cores[1])

    result = await db.execute(
        select(node_additional_cores_association.c.node_id).where(
            node_additional_cores_association.c.core_config_id == core_id
        )
    )
    assert result.scalars().all() == [], "a deleted core must not leave rows that a reused core id could inherit"
    assert node.id is not None


@pytest.mark.asyncio
async def test_the_response_model_emits_a_literal_null_not_an_empty_list(db, cores):
    from app.models.node import NodeResponse

    node = await create_node(
        db, NodeCreate(**payload("h-null-shape", 64508, cores[0].id, None, "44444444-4444-4444-4444-444444444444"))
    )
    dumped = NodeResponse.model_validate(node).model_dump()
    assert "additional_core_config_ids" in dumped
    assert dumped["additional_core_config_ids"] is None, (
        "the API emitted null before this feature existed and must keep doing so"
    )


@pytest.mark.asyncio
async def test_the_explicit_sweep_is_what_protects_us_not_the_cascade(db, cores):
    from sqlalchemy import text

    enforced = (await db.execute(text("PRAGMA foreign_keys"))).scalar()
    assert enforced == 0, (
        "sqlite leaves foreign keys off, so ON DELETE CASCADE never fires here - "
        "the explicit sweeps in remove_node/remove_core_config are the real protection"
    )

    node = await create_node(
        db, NodeCreate(**payload("h-pragma", 64510, cores[0].id, [cores[1].id], "55555555-5555-5555-5555-555555555555"))
    )
    node_id = node.id
    assert await _rows_for_node(db, node_id) == [cores[1].id]

    await remove_node(db, node)
    assert await _rows_for_node(db, node_id) == [], (
        "with the cascade inert, the row can only be gone because the sweep removed it"
    )
