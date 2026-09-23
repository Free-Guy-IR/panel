from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.db.models import NodeUserUsage, UserSubscriptionUpdate
from tests.api import TestSession, client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    create_group,
    create_user,
    delete_admin,
    delete_core,
    delete_group,
    unique_name,
)


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _aggregates(token: str, window: dict) -> tuple[int, int, int, set[str]]:
    usage = client.get("/api/users/usage", headers=auth_headers(token), params=window)
    online = client.get("/api/users/counts/online", headers=auth_headers(token), params=window)
    chart = client.get("/api/users/sub_update/chart", headers=auth_headers(token), params=window)
    assert (usage.status_code, online.status_code, chart.status_code) == (200, 200, 200), (
        usage.text,
        online.text,
        chart.text,
    )
    traffic = sum(stat["total_traffic"] for stats in usage.json()["stats"].values() for stat in stats)
    return (
        traffic,
        online.json()["count_during_period"],
        chart.json()["total"],
        {segment["name"] for segment in chart.json()["segments"]},
    )


async def test_a_group_restricted_admin_only_aggregates_users_it_can_see(access_token):
    core = create_core(access_token)
    allowed = create_group(access_token, name=unique_name("agg_allowed"))
    forbidden = create_group(access_token, name=unique_name("agg_forbidden"))
    role = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("agg_role"),
            "permissions": {"users": {"read": True}},
            "access": {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": [allowed["id"]]},
        },
    )
    assert role.status_code == 201, role.text
    admin = create_admin(access_token, role_id=role.json()["id"])
    token = _login(admin["username"], admin["password"])
    visible = create_user(access_token, group_ids=[allowed["id"]], payload={"username": unique_name("agg_visible")})
    hidden = create_user(access_token, group_ids=[forbidden["id"]], payload={"username": unique_name("agg_hidden")})
    at = (datetime.now(UTC) - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
    async with TestSession() as session:
        session.add_all(
            [
                NodeUserUsage(created_at=at, user_id=visible["id"], node_id=None, used_traffic=100),
                NodeUserUsage(created_at=at, user_id=hidden["id"], node_id=None, used_traffic=10_000),
                UserSubscriptionUpdate(user_id=visible["id"], user_agent="v2rayNG/1.8"),
                UserSubscriptionUpdate(user_id=hidden["id"], user_agent="Hiddify/2.0"),
            ]
        )
        await session.commit()
    window = {
        "period": "day",
        "start": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "end": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    try:
        owner_view = _aggregates(access_token, window)
        restricted_view = _aggregates(token, window)
        assert owner_view[0] >= 10_100 and owner_view[1] >= 2 and owner_view[2] >= 2, owner_view
        assert restricted_view[:3] == (100, 1, 1), restricted_view
        assert all("Hiddify" not in name for name in restricted_view[3]), restricted_view
    finally:
        async with TestSession() as session:
            ids = [visible["id"], hidden["id"]]
            await session.execute(delete(NodeUserUsage).where(NodeUserUsage.user_id.in_(ids)))
            await session.execute(delete(UserSubscriptionUpdate).where(UserSubscriptionUpdate.user_id.in_(ids)))
            await session.commit()
        for user in (visible, hidden):
            client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_admin(access_token, admin["username"])
        client.delete(f"/api/admin-role/{role.json()['id']}", headers=auth_headers(access_token))
        delete_group(access_token, allowed["id"])
        delete_group(access_token, forbidden["id"])
        delete_core(access_token, core["id"])
