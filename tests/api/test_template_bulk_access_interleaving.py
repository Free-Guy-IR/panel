import asyncio

import pytest
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import UserTemplate
from app.models.user_template import BulkUserTemplateSelection
from app.operation import OperatorType
from app.operation.user_template import UserTemplateOperation
from app.routers.authentication import get_admin
from tests.api import DATABASE_URL, IS_SQLITE, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    create_group,
    create_user_template,
    delete_admin,
    delete_core,
    delete_group,
)
from tests.api.test_permission_leaks import _create_role, _delete_role, _login


def _own_connection_sessions():
    connect_args = {"timeout": 30} if IS_SQLITE else {}
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args=connect_args)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def interleaved_catalog(access_token):
    core = create_core(access_token)
    allowed_group = create_group(access_token)
    hidden_group = create_group(access_token)
    allowed_template = create_user_template(access_token, group_ids=[allowed_group["id"]])
    hidden_templates = [create_user_template(access_token, group_ids=[hidden_group["id"]]) for _ in range(3)]
    role = _create_role(
        access_token,
        {"templates": {"read": True, "read_simple": True, "update": True, "delete": True}},
        access={"allowed_template_ids": [allowed_template["id"]]},
    )
    actor = create_admin(access_token, role_id=role["id"])
    try:
        yield {
            "actor_token": _login(actor["username"], actor["password"]),
            "owner_token": access_token,
            "allowed_template": allowed_template,
            "hidden_templates": hidden_templates,
        }
    finally:
        delete_admin(access_token, actor["username"])
        _delete_role(access_token, role["id"])
        for template in [allowed_template, *hidden_templates]:
            client.delete(f"/api/user_template/{template['id']}", headers=auth_headers(access_token))
        for group in (allowed_group, hidden_group):
            delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


@pytest.mark.skipif(":memory:" in DATABASE_URL, reason="needs a database shared by several connections")
def test_concurrent_restricted_bulk_actions_never_touch_templates_outside_the_allowlist(interleaved_catalog):
    hidden_ids = [template["id"] for template in interleaved_catalog["hidden_templates"]]
    every_id = [interleaved_catalog["allowed_template"]["id"], *hidden_ids]
    operation = UserTemplateOperation(operator_type=OperatorType.API)

    async def _scenario() -> tuple[list, list, dict]:
        engine, sessions = _own_connection_sessions()
        async with sessions() as db:
            actor = await get_admin(db, interleaved_catalog["actor_token"])
            owner = await get_admin(db, interleaved_catalog["owner_token"])
            await db.rollback()
        assert actor is not None and owner is not None

        async def _restricted(step: int) -> str:
            await asyncio.sleep(0.001 * step)
            selection = BulkUserTemplateSelection(ids={hidden_ids[step % len(hidden_ids)]})
            async with sessions() as db:
                try:
                    if step % 2 == 0:
                        await operation.bulk_remove_user_templates(db, selection, actor)
                    else:
                        await operation.bulk_set_user_templates_disabled(db, selection, actor, is_disabled=True)
                    await db.commit()
                    return "ok"
                except HTTPException as exc:
                    await db.rollback()
                    return str(exc.status_code)

        async def _empty_selection(step: int, admin) -> str:
            await asyncio.sleep(0.0015 * step)
            async with sessions() as db:
                if step % 2 == 0:
                    response = await operation.bulk_remove_user_templates(db, BulkUserTemplateSelection(), admin)
                else:
                    response = await operation.bulk_set_user_templates_disabled(
                        db, BulkUserTemplateSelection(), admin, is_disabled=True
                    )
                await db.commit()
                return f"count={response.count}"

        async def _owner_reenables(step: int) -> str:
            await asyncio.sleep(0.002 * step)
            async with sessions() as db:
                try:
                    await operation.bulk_set_user_templates_disabled(
                        db, BulkUserTemplateSelection(ids=set(every_id)), owner, is_disabled=False
                    )
                    await db.commit()
                    return "ok"
                except HTTPException as exc:
                    await db.rollback()
                    return str(exc.status_code)

        restricted = await asyncio.gather(
            *[_restricted(step) for step in range(8)],
            *[_owner_reenables(step) for step in range(3)],
        )
        empties = await asyncio.gather(
            *[_empty_selection(step, actor) for step in range(4)],
            *[_empty_selection(step, owner) for step in range(4)],
        )
        async with sessions() as db:
            rows = (
                await db.execute(select(UserTemplate.id, UserTemplate.is_disabled).where(UserTemplate.id.in_(every_id)))
            ).all()
            await db.rollback()
        await engine.dispose()
        return restricted, empties, {row[0]: row[1] for row in rows}

    restricted, empties, final_rows = asyncio.run(_scenario())

    assert restricted[:8] == [str(status.HTTP_404_NOT_FOUND)] * 8
    assert restricted[8:] == ["ok"] * 3
    assert empties == ["count=0"] * 8
    assert sorted(final_rows) == sorted(every_id)
    assert not any(final_rows.values())
