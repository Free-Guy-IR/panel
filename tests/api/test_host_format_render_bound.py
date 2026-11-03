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

WS_INBOUND = "VLESS WebSocket TEST"
TLS_INBOUND = "Trojan Websocket TLS"
WIDTH = 200000
CLIENT_TYPES = ("links", "xray", "sing_box", "clash_meta")
TROJAN_ONLY_CLIENT_TYPES = ("clash",)


async def _store_unvalidated_fields(host_id: int, **fields) -> None:
    async with GetTestDB() as db:
        db_host = await get_host_by_id(db, host_id)
        for name, value in fields.items():
            setattr(db_host, name, value)
        await db.commit()
        await host_manager.add_hosts(db, await get_hosts(db=db))


@pytest.mark.parametrize(
    ("inbound_tag", "fields", "marker"),
    [
        (WS_INBOUND, {"address": {f"addrmark{{SERVER_IP:>{WIDTH}}}"}}, "addrmark"),
        (WS_INBOUND, {"remark": f"remarkmark{{USERNAME:>{WIDTH}}}"}, "remarkmark"),
        (TLS_INBOUND, {"sni": {f"{{USERNAME:>{WIDTH}}}.snimark.example.com"}}, "snimark"),
        (WS_INBOUND, {"host": {f"{{USERNAME:>{WIDTH}}}.hostmark.example.com"}}, "hostmark"),
        (WS_INBOUND, {"path": f"/ws/pathmark{{USERNAME:>{WIDTH}}}"}, "pathmark"),
        (WS_INBOUND, {"address": {f"bracemark{{SERVER_IP:>{WIDTH}}}{{"}}, "bracemark"),
    ],
)
def test_a_stored_oversized_format_width_is_never_padded_on_render(access_token, inbound_tag, fields, marker):
    headers = auth_headers(access_token)
    core = create_core(access_token)
    group = create_group(access_token, inbound_tags=[inbound_tag])
    user = create_user(access_token, group_ids=[group["id"]])
    host_id = None
    try:
        response = client.post(
            "/api/host",
            headers=headers,
            json={
                "remark": unique_name("wide"),
                "address": ["wide.example.com"],
                "inbound_tag": inbound_tag,
                "priority": 1,
                "path": "/ws",
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        host_id = response.json()["id"]
        asyncio.run(_store_unvalidated_fields(host_id, **fields))

        client_types = CLIENT_TYPES + (TROJAN_ONLY_CLIENT_TYPES if inbound_tag == TLS_INBOUND else ())
        for client_type in client_types:
            config_cache.clear_sub_config_cache()
            response = client.get(f"{user['subscription_url']}/{client_type}")
            assert response.status_code == status.HTTP_200_OK, (client_type, response.text[:200])
            assert (marker in response.text) is ("address" not in fields), (client_type, response.text[:400])
            assert " " * 1000 not in response.text, client_type
            assert len(response.content) < WIDTH, (client_type, len(response.content))
    finally:
        config_cache.clear_sub_config_cache()
        if host_id is not None:
            client.delete(f"/api/host/{host_id}", headers=headers)
        delete_user(access_token, user["username"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
