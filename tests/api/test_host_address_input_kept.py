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


def _addresses_joined_to(length: int) -> list[str]:
    head = [f"edge{index:02d}.example.com" for index in range(10)]
    tail = length - len(",".join(head)) - 1 - len(".example.com")
    addresses = [*head, "x" * tail + ".example.com"]
    assert len(",".join(addresses)) == length
    return addresses


def _remarks(access_token: str) -> set[str]:
    response = client.get("/api/hosts", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK, response.text
    return {host["remark"] for host in response.json()}


def _stored_host_at_the_column_length(access_token: str) -> tuple[dict, int, list[str], str]:
    core = create_core(access_token, name=unique_name("address_core"))
    inbound = get_inbounds(access_token)[0]
    headers = auth_headers(access_token)
    fits = _addresses_joined_to(256)
    created = client.post("/api/host", headers=headers, json=_host_body(inbound, fits))
    assert created.status_code == status.HTTP_201_CREATED, created.text
    host_id = created.json()["id"]
    stored = client.get(f"/api/host/{host_id}", headers=headers)
    assert stored.status_code == status.HTTP_200_OK, stored.text
    assert sorted(stored.json()["address"]) == sorted(fits)
    return core, host_id, fits, inbound


def test_host_address_list_at_the_column_length_is_stored_and_one_character_more_is_refused_on_create(access_token):
    core, host_id, _, inbound = _stored_host_at_the_column_length(access_token)
    try:
        refused_body = _host_body(inbound, _addresses_joined_to(257))
        response = client.post("/api/host", headers=auth_headers(access_token), json=refused_body)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
        assert "256" in response.json()["detail"]["address"]
        assert refused_body["remark"] not in _remarks(access_token)
    finally:
        client.delete(f"/api/host/{host_id}", headers=auth_headers(access_token))
        delete_core(access_token, core["id"])


def test_host_address_list_one_character_over_the_column_length_is_refused_on_a_batch_save(access_token):
    core, host_id, fits, _ = _stored_host_at_the_column_length(access_token)
    headers = auth_headers(access_token)
    try:
        stored = client.get(f"/api/host/{host_id}", headers=headers).json()
        response = client.put("/api/hosts", headers=headers, json=[{**stored, "address": _addresses_joined_to(257)}])
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
        assert "256" in response.json()["detail"]["address"]

        kept = client.get(f"/api/host/{host_id}", headers=headers)
        assert kept.status_code == status.HTTP_200_OK, kept.text
        assert sorted(kept.json()["address"]) == sorted(fits)
    finally:
        client.delete(f"/api/host/{host_id}", headers=headers)
        delete_core(access_token, core["id"])
