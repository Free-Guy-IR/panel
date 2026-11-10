import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_core, delete_core, unique_name

INBOUND_TAG = "VLESS WebSocket TEST"


@pytest.mark.parametrize(
    "address",
    ["{SERVER_IP:300}", "{SERVER_IP:1000000000}", "{SERVER_IP.real}", "{SERVER_IP[0]}", "edge{0}.example.com"],
)
def test_an_address_the_fork_character_rule_allows_is_still_format_checked(access_token, address):
    headers = auth_headers(access_token)
    core = create_core(access_token)
    payload = {"remark": unique_name("fmt"), "address": [address], "inbound_tag": INBOUND_TAG, "priority": 1}
    try:
        response = client.post("/api/host", headers=headers, json=payload)
        if response.status_code == status.HTTP_201_CREATED:
            client.delete(f"/api/host/{response.json()['id']}", headers=headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
    finally:
        delete_core(access_token, core["id"])


MARKUP_ADDRESSES = [
    "<img src=x onerror=alert(1)>",
    "a'b.example.com",
    'a"b.example.com',
    "a b.example.com",
    "a`b",
    "a\\b",
    "<x>",
]

LITERAL_ADDRESSES = ["example.com", "*.example.com", "{SERVER_IP}", "[2001:db8::1]", "{USERNAME}.example.com"]


def test_the_fork_address_character_rule_rejects_markup_and_keeps_literal_forms(access_token):
    headers = auth_headers(access_token)
    core = create_core(access_token)
    created: list[int] = []
    try:
        for address in MARKUP_ADDRESSES:
            payload = {"remark": unique_name("mk"), "address": [address], "inbound_tag": INBOUND_TAG, "priority": 1}
            response = client.post("/api/host", headers=headers, json=payload)
            if response.status_code == status.HTTP_201_CREATED:
                created.append(response.json()["id"])
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, (address, response.text)

        for address in LITERAL_ADDRESSES:
            payload = {"remark": unique_name("lit"), "address": [address], "inbound_tag": INBOUND_TAG, "priority": 1}
            response = client.post("/api/host", headers=headers, json=payload)
            assert response.status_code == status.HTTP_201_CREATED, (address, response.text)
            created.append(response.json()["id"])
    finally:
        for host_id in created:
            client.delete(f"/api/host/{host_id}", headers=headers)
        delete_core(access_token, core["id"])
