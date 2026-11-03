import asyncio

import pytest
from fastapi import status

from app.core.hosts import host_manager
from app.db.crud.host import get_host_by_id, get_hosts
from app.subscription import config_cache
from tests.api import GetTestDB, client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)

INBOUND_TAG = "VLESS WebSocket TEST"
WIDTH = 200000


async def _store_unvalidated_fields(host_id: int, **fields) -> None:
    async with GetTestDB() as db:
        db_host = await get_host_by_id(db, host_id)
        for name, value in fields.items():
            setattr(db_host, name, value)
        await db.commit()
        await host_manager.add_hosts(db, await get_hosts(db=db))


@pytest.mark.parametrize(
    "fields",
    [
        {"address": {f"{{SERVER_IP:>{WIDTH}}}"}},
        {"remark": f"{{USERNAME:>{WIDTH}}}"},
        {"sni": {f"{{USERNAME:>{WIDTH}}}.example.com"}},
        {"host": {f"{{USERNAME:>{WIDTH}}}.example.com"}},
        {"path": f"/ws/{{USERNAME:>{WIDTH}}}"},
        {"address": {f"{{SERVER_IP:>{WIDTH}}}{{"}},
    ],
)
def test_a_stored_oversized_format_width_is_never_padded_on_render(access_token, fields):
    headers = auth_headers(access_token)
    core = create_core(access_token)
    group = create_group(access_token, inbound_tags=[INBOUND_TAG])
    user = create_user(access_token, group_ids=[group["id"]])
    host_id = None
    try:
        response = client.post(
            "/api/host",
            headers=headers,
            json={
                "remark": unique_name("wide"),
                "address": ["wide.example.com"],
                "inbound_tag": INBOUND_TAG,
                "priority": 1,
                "path": "/ws",
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        host_id = response.json()["id"]
        asyncio.run(_store_unvalidated_fields(host_id, **fields))

        for client_type in ("links", "xray", "sing_box", "clash"):
            config_cache.clear_sub_config_cache()
            response = client.get(f"{user['subscription_url']}/{client_type}")
            assert response.status_code == status.HTTP_200_OK, (client_type, response.text[:200])
            assert " " * 1000 not in response.text, client_type
            assert len(response.content) < WIDTH, (client_type, len(response.content))
    finally:
        config_cache.clear_sub_config_cache()
        if host_id is not None:
            client.delete(f"/api/host/{host_id}", headers=headers)
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
