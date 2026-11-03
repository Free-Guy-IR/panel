from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_core, delete_core, get_inbounds, unique_name


def _create_host(access_token: str, inbound: str) -> dict:
    response = client.post(
        "/api/host",
        headers=auth_headers(access_token),
        json={
            "remark": unique_name("atomic_host"),
            "address": ["example.com"],
            "port": 443,
            "inbound_tag": inbound,
            "priority": 1,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def test_bulk_toggle_without_a_selection_is_rejected_and_touches_no_host(access_token):
    core = create_core(access_token, name=unique_name("atomic_core"))
    inbound = get_inbounds(access_token)[0]
    hosts = [_create_host(access_token, inbound) for _ in range(2)]
    try:
        for body in ({}, {"ids": []}):
            response = client.post("/api/hosts/bulk/disable", headers=auth_headers(access_token), json=body)
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text

        for host in hosts:
            stored = client.get(f"/api/host/{host['id']}", headers=auth_headers(access_token))
            assert stored.json()["is_disabled"] is False
    finally:
        client.post(
            "/api/hosts/bulk/delete", headers=auth_headers(access_token), json={"ids": [h["id"] for h in hosts]}
        )
        delete_core(access_token, core["id"])


def test_a_host_batch_with_one_invalid_host_saves_nothing(access_token):
    core = create_core(access_token, name=unique_name("atomic_core"))
    inbound = get_inbounds(access_token)[0]
    host = _create_host(access_token, inbound)
    original_remark = host["remark"]
    try:
        changed = {**host, "remark": unique_name("atomic_renamed")}
        broken = {
            "remark": unique_name("atomic_broken"),
            "address": ["example.com"],
            "port": 443,
            "inbound_tag": inbound,
            "priority": 2,
            "transport_settings": {"xhttp_settings": {"download_settings": 987654321}},
        }
        response = client.put("/api/hosts", headers=auth_headers(access_token), json=[changed, broken])
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text

        stored = client.get(f"/api/host/{host['id']}", headers=auth_headers(access_token))
        assert stored.json()["remark"] == original_remark
        listed = client.get("/api/hosts", headers=auth_headers(access_token)).json()
        assert broken["remark"] not in {item["remark"] for item in listed}
    finally:
        client.post("/api/hosts/bulk/delete", headers=auth_headers(access_token), json={"ids": [host["id"]]})
        delete_core(access_token, core["id"])
