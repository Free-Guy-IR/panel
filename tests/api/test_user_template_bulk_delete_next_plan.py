from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import NextPlan
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    create_user_template,
    delete_core,
    delete_group,
    unique_name,
)

GB = 1024**3


async def test_bulk_deleting_the_template_behind_a_next_plan_does_not_turn_it_into_an_unlimited_plan(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("np_orphan_group"))
    template = create_user_template(
        access_token, group_ids=[group["id"]], data_limit=5 * GB, expire_duration=30 * 86400
    )
    user = create_user(
        access_token,
        group_ids=[group["id"]],
        payload={
            "username": unique_name("np_orphan"),
            "data_limit": GB,
            "expire": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "next_plan": {"user_template_id": template["id"]},
        },
    )
    try:
        async with TestSession() as session:
            stored = (
                await session.execute(
                    select(NextPlan.user_template_id, NextPlan.data_limit, NextPlan.expire).where(
                        NextPlan.user_id == user["id"]
                    )
                )
            ).first()
        removed = client.post(
            "/api/user_templates/bulk/delete", headers=auth_headers(access_token), json={"ids": [template["id"]]}
        )
        assert removed.status_code == 200, removed.text
        activated = client.post(f"/api/user/by-id/{user['id']}/active_next", headers=auth_headers(access_token))
        body = activated.json()
        assert activated.status_code != 200 or (body["data_limit"] not in (0, None) and body["expire"] is not None), (
            f"next_plan row at creation={tuple(stored)}; template deleted; active_next={activated.status_code} "
            f"gave data_limit={body.get('data_limit')} expire={body.get('expire')} status={body.get('status')}: "
            f"an unlimited, never-expiring user"
        )
    finally:
        client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


async def test_bulk_and_single_template_delete_leave_the_same_next_plan_state(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("np_orphan_group"))
    templates = [
        create_user_template(access_token, group_ids=[group["id"]], data_limit=5 * GB, expire_duration=30 * 86400)
        for _ in range(2)
    ]
    users = [
        create_user(
            access_token,
            group_ids=[group["id"]],
            payload={"username": unique_name("np_orphan"), "next_plan": {"user_template_id": template["id"]}},
        )
        for template in templates
    ]
    try:
        bulk = client.post(
            "/api/user_templates/bulk/delete", headers=auth_headers(access_token), json={"ids": [templates[0]["id"]]}
        )
        single = client.delete(f"/api/user_template/{templates[1]['id']}", headers=auth_headers(access_token))
        assert (bulk.status_code, single.status_code) == (200, 204), (bulk.text, single.text)
        async with TestSession() as session:
            remaining = (
                await session.execute(select(NextPlan.user_id).where(NextPlan.user_id.in_([u["id"] for u in users])))
            ).all()
        assert remaining == []
        for user in users:
            activated = client.post(f"/api/user/by-id/{user['id']}/active_next", headers=auth_headers(access_token))
            assert activated.status_code == 404, activated.text
    finally:
        for user in users:
            client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
