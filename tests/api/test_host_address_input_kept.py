from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_core, delete_core, get_inbounds, unique_name

MARKUP_PAYLOAD = '"><img src=x onerror="window.__xss=1">'
UNSAFE_ADDRESSES = (
    MARKUP_PAYLOAD,
    "a'b.example.com",
    'a"b.example.com',
    "a b.example.com",
    "a`b",
    "a\\b",
    "<x>",
    "{SERVER_IP!r}",
)
UPSTREAM_ACCEPTED_ADDRESSES = ("example.com", "*.example.com", "{SERVER_IP}", "[2001:db8::1]", "{USERNAME}.example.com")


def _host_body(inbound: str, addresses: list[str]) -> dict:
    return {
        "remark": unique_name("address_host"),
        "address": addresses,
        "port": 443,
        "inbound_tag": inbound,
        "priority": 1,
    }


def _oversized_addresses() -> list[str]:
    return [f"edge{index:02d}.{'a' * 20}.example.com" for index in range(10)]


def test_host_address_over_column_length_is_refused_on_create_and_update(access_token):
    core = create_core(access_token, name=unique_name("address_core"))
    inbound = get_inbounds(access_token)[0]
    created = client.post("/api/host", headers=auth_headers(access_token), json=_host_body(inbound, ["example.com"]))
    assert created.status_code == status.HTTP_201_CREATED, created.text
    host_id = created.json()["id"]
    try:
        oversized = _oversized_addresses()
        assert len(",".join(oversized)) > 256

        response = client.post("/api/host", headers=auth_headers(access_token), json=_host_body(inbound, oversized))
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text

        response = client.put(
            f"/api/host/{host_id}", headers=auth_headers(access_token), json=_host_body(inbound, oversized)
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text

        stored = client.get(f"/api/host/{host_id}", headers=auth_headers(access_token))
        assert stored.status_code == status.HTTP_200_OK
        assert stored.json()["address"] == ["example.com"]
    finally:
        client.delete(f"/api/host/{host_id}", headers=auth_headers(access_token))
        delete_core(access_token, core["id"])


def test_host_address_with_markup_is_refused_and_upstream_safe_values_are_kept(access_token):
    core = create_core(access_token, name=unique_name("address_core"))
    inbound = get_inbounds(access_token)[0]
    created: list[int] = []
    try:
        for address in UNSAFE_ADDRESSES:
            response = client.post("/api/host", headers=auth_headers(access_token), json=_host_body(inbound, [address]))
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, (address, response.text)

        for address in UPSTREAM_ACCEPTED_ADDRESSES:
            response = client.post("/api/host", headers=auth_headers(access_token), json=_host_body(inbound, [address]))
            assert response.status_code == status.HTTP_201_CREATED, (address, response.text)
            created.append(response.json()["id"])
    finally:
        for host_id in created:
            client.delete(f"/api/host/{host_id}", headers=auth_headers(access_token))
        delete_core(access_token, core["id"])
