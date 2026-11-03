import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers
from tests.api.test_node import node_create_payload, node_operator_mock, sample_node_response  # noqa: F401

SECRET = "pw" + "Leak" + "Marker" + str(7)
USER = "u" + "x"
HOST = "proxy.example"
BREAKING = ["[", "]", "\\", "/", "?", "#"]


def _bad_urls() -> list[str]:
    urls = []
    for char in BREAKING:
        urls.append(f"http://{USER}:{SECRET}{char}@{HOST}:8080")
        urls.append(f"socks5://{USER}{char}:{SECRET}@{HOST}:1080")
    return urls


BAD_URLS = _bad_urls()


def _proxy_sections(payload: dict) -> list[str]:
    return [name for name, section in payload.items() if isinstance(section, dict) and "proxy_url" in section]


@pytest.mark.parametrize("url", BAD_URLS)
def test_settings_api_rejects_breaking_proxy_userinfo(access_token, url):
    before = client.get("/api/settings", headers=auth_headers(access_token))
    assert before.status_code == status.HTTP_200_OK
    sections = _proxy_sections(before.json())
    assert len(sections) >= 3
    for section in sections:
        body = {section: {**before.json()[section], "proxy_url": url}}
        response = client.put("/api/settings", headers=auth_headers(access_token), json=body)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY, section
        assert SECRET not in response.text
        after = client.get("/api/settings", headers=auth_headers(access_token))
        assert after.json()[section]["proxy_url"] == before.json()[section]["proxy_url"]


@pytest.mark.parametrize("url", BAD_URLS)
def test_node_create_rejects_breaking_proxy_userinfo(access_token, node_operator_mock, url):  # noqa: F811
    payload = node_create_payload(proxy_url=url)
    response = client.post("/api/node", headers=auth_headers(access_token), json=payload)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert SECRET not in response.text
    node_operator_mock.create_node.assert_not_awaited()


@pytest.mark.parametrize("url", BAD_URLS)
def test_node_modify_rejects_breaking_proxy_userinfo(access_token, node_operator_mock, url):  # noqa: F811
    node_operator_mock.get_validated_node.return_value = sample_node_response(id=31, name="kept-node")
    response = client.put("/api/node/31", headers=auth_headers(access_token), json={"proxy_url": url})
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert SECRET not in response.text
    node_operator_mock.modify_node.assert_not_awaited()
