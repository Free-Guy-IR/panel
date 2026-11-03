from fastapi import status

import app.core.hosts as hosts_module
from app.core.hosts import host_manager
from tests.api import client
from tests.api.helpers import auth_headers, create_core, delete_core, get_inbounds, unique_name


def _create_disabled_hosts(access_token: str, count: int) -> list[int]:
    inbound = get_inbounds(access_token)[0]
    host_ids = []
    for _ in range(count):
        response = client.post(
            "/api/host",
            headers=auth_headers(access_token),
            json={
                "remark": unique_name("isolated_host"),
                "address": ["example.com"],
                "port": 443,
                "inbound_tag": inbound,
                "priority": 1,
                "is_disabled": True,
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        host_ids.append(response.json()["id"])
    return host_ids


def test_bulk_enable_skips_a_host_that_cannot_be_prepared_and_serves_the_rest(access_token, monkeypatch):
    core = create_core(access_token, name=unique_name("isolated_core"))
    host_ids = _create_disabled_hosts(access_token, 3)
    broken = host_ids[1]
    real_prepare = hosts_module._prepare_subscription_inbound_data

    async def prepare(host, *args, **kwargs):
        if host.id == broken:
            raise TypeError("unpreparable host")
        return await real_prepare(host, *args, **kwargs)

    monkeypatch.setattr(hosts_module, "_prepare_subscription_inbound_data", prepare)
    try:
        response = client.post("/api/hosts/bulk/enable", headers=auth_headers(access_token), json={"ids": host_ids})
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["count"] == 3

        for host_id in host_ids:
            stored = client.get(f"/api/host/{host_id}", headers=auth_headers(access_token))
            assert stored.json()["is_disabled"] is False
        assert host_ids[0] in host_manager._hosts
        assert host_ids[2] in host_manager._hosts
        assert broken not in host_manager._hosts
    finally:
        monkeypatch.undo()
        client.post("/api/hosts/bulk/delete", headers=auth_headers(access_token), json={"ids": host_ids})
        delete_core(access_token, core["id"])
