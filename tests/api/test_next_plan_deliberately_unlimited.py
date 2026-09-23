from datetime import UTC, datetime, timedelta

from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    unique_name,
)


def test_owner_set_unlimited_next_plan_is_applied(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("fv_unl_group"))
    user = create_user(
        access_token,
        group_ids=[group["id"]],
        payload={
            "username": unique_name("fv_unl"),
            "data_limit": 1024,
            "expire": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "next_plan": {"data_limit": 0, "expire": 0, "add_remaining_traffic": False},
        },
    )
    try:
        activated = client.post(f"/api/user/by-id/{user['id']}/active_next", headers=auth_headers(access_token))
        body = activated.json()
        assert activated.status_code == 200 and body["data_limit"] in (0, None) and body["expire"] is None, body
    finally:
        client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
