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

NEXT_PLAN = {"data_limit": 2048, "expire": 86400, "add_remaining_traffic": False}


def test_partial_update_keeps_omitted_next_plan_and_null_removes_it(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("np_keep_group"))
    user = create_user(
        access_token,
        group_ids=[group["id"]],
        payload={
            "username": unique_name("np_keep"),
            "data_limit": 1024,
            "expire": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "next_plan": NEXT_PLAN,
        },
    )
    try:
        renamed = client.put(
            f"/api/user/{user['username']}", headers=auth_headers(access_token), json={"note": "partial"}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["note"] == "partial"
        assert renamed.json()["next_plan"] is not None, renamed.json()
        assert renamed.json()["next_plan"]["data_limit"] == NEXT_PLAN["data_limit"]

        cleared = client.put(
            f"/api/user/{user['username']}", headers=auth_headers(access_token), json={"next_plan": None}
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["next_plan"] is None, cleared.json()
    finally:
        client.delete(f"/api/user/by-id/{user['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
