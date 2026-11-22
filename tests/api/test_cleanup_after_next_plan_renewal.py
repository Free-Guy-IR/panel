from datetime import UTC, datetime as dt, timedelta as td

import pytest
from fastapi import status
from sqlalchemy import select

from app.db.models import User
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    create_user_template,
    delete_core,
    delete_group,
    delete_user,
    delete_user_template,
    unique_name,
)

CUTOFF_DAYS = 30


async def _age(user_id: int, *, online_ago: td | None, status_changed_ago: td | None = None) -> None:
    now = dt.now(UTC)
    async with TestSession() as session:
        db_user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        db_user.created_at = now - td(days=400)
        db_user.online_at = None if online_ago is None else now - online_ago
        db_user.edit_at = now - td(days=200)
        if status_changed_ago is not None:
            db_user.last_status_change = now - status_changed_ago
        await session.commit()


async def _stored(user_id: int):
    async with TestSession() as session:
        return (
            await session.execute(select(User.status, User.online_at, User.last_status_change).where(User.id == user_id))
        ).one_or_none()


def _cutoff() -> str:
    return (dt.now(UTC) - td(days=CUTOFF_DAYS)).isoformat()


def _listed(access_token, target: str) -> set[str]:
    response = client.get(
        "/api/users/expired",
        params={"target": target, "expired_before": _cutoff()},
        headers=auth_headers(access_token),
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return set(response.json())


def _delete_target(access_token, target: str) -> None:
    response = client.delete(
        "/api/users/expired",
        params={"target": target, "expired_before": _cutoff()},
        headers=auth_headers(access_token),
    )
    assert response.status_code in (status.HTTP_200_OK, status.HTTP_404_NOT_FOUND), response.text


def _renew(access_token, username: str, template_id: int, expected_status: str) -> None:
    update = client.put(
        f"/api/user/{username}",
        headers=auth_headers(access_token),
        json={"next_plan": {"user_template_id": template_id}},
    )
    assert update.status_code == status.HTTP_200_OK, update.text
    renewed = client.post(f"/api/user/{username}/active_next", headers=auth_headers(access_token))
    assert renewed.status_code == status.HTTP_200_OK, renewed.text
    assert renewed.json()["status"] == expected_status


def _world(access_token, template_status: str):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("renewed_cleanup_group"))
    template_kwargs = {"group_ids": [group["id"]], "expire_duration": 86400 * 30}
    if template_status == "on_hold":
        template_kwargs.update(status_value="on_hold", on_hold_timeout=86400 * 7)
    template = create_user_template(access_token, **template_kwargs)
    return core, group, template


async def _drop_world(access_token, users, core, group, template) -> None:
    for user in users:
        if await _stored(user["id"]) is not None:
            delete_user(access_token, user["username"])
    delete_user_template(access_token, template["id"])
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


@pytest.mark.asyncio
async def test_a_renewal_into_an_on_hold_plan_starts_a_new_hold_for_the_on_hold_cleanup(access_token):
    core, group, template = _world(access_token, "on_hold")
    renewed = create_user(
        access_token, group_ids=[group["id"]], payload={"username": unique_name("renewed_on_hold")}
    )
    held = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("held_on_hold")})
    try:
        await _age(renewed["id"], online_ago=td(hours=1), status_changed_ago=td(days=100))
        _renew(access_token, renewed["username"], template["id"], "on_hold")
        _renew(access_token, held["username"], template["id"], "on_hold")
        await _age(held["id"], online_ago=None, status_changed_ago=td(days=100))

        listed = _listed(access_token, "on_hold")
        _delete_target(access_token, "on_hold")

        assert renewed["username"] not in listed, "a user put on hold by its renewal a moment ago is listed"
        assert await _stored(renewed["id"]) is not None, (
            "a user put on hold by its renewal a moment ago was deleted as on hold for more than thirty days"
        )
        assert held["username"] in listed, "a user on hold for a hundred days must stay an on_hold target"
        assert await _stored(held["id"]) is None, "the on_hold cleanup must still delete a user on hold for 100 days"
    finally:
        await _drop_world(access_token, [renewed, held], core, group, template)
