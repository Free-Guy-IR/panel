import pytest
from fastapi import status

from app.core.hosts import host_manager
from tests.api import client
from tests.api.helpers import auth_headers, create_core, delete_core, get_inbounds, unique_name


def _create_hosts(access_token: str, count: int) -> list[int]:
    inbound = get_inbounds(access_token)[0]
    host_ids = []
    for _ in range(count):
        response = client.post(
            "/api/host",
            headers=auth_headers(access_token),
            json={
                "remark": unique_name("bulk_host"),
                "address": ["example.com"],
                "port": 443,
                "inbound_tag": inbound,
                "priority": 1,
            },
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        host_ids.append(response.json()["id"])
    return host_ids


@pytest.mark.parametrize("action, disabled", [("disable", True), ("enable", False)])
def test_bulk_toggle_refreshes_the_host_manager_once(access_token, monkeypatch, action, disabled):
    core = create_core(access_token, name=unique_name("bulk_core"))
    host_ids = _create_hosts(access_token, 3)
    if not disabled:
        response = client.post("/api/hosts/bulk/disable", headers=auth_headers(access_token), json={"ids": host_ids})
        assert response.status_code == status.HTTP_200_OK, response.text

    batches: list[list[int]] = []
    real_add_hosts = host_manager.add_hosts

    async def recording_add_hosts(db, hosts):
        batches.append(sorted(host.id for host in hosts))
        await real_add_hosts(db, hosts)

    monkeypatch.setattr(host_manager, "add_hosts", recording_add_hosts)
    try:
        response = client.post(f"/api/hosts/bulk/{action}", headers=auth_headers(access_token), json={"ids": host_ids})
        assert response.status_code == status.HTTP_200_OK, response.text
        assert response.json()["count"] == 3
        assert batches == [sorted(host_ids)]

        for host_id in host_ids:
            stored = client.get(f"/api/host/{host_id}", headers=auth_headers(access_token))
            assert stored.json()["is_disabled"] is disabled
    finally:
        monkeypatch.undo()
        client.post("/api/hosts/bulk/delete", headers=auth_headers(access_token), json={"ids": host_ids})
        delete_core(access_token, core["id"])
