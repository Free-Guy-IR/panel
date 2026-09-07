import json

import pytest

from app.models.settings import ConfigFormat
from tests.api import client
from tests.api.helpers import (
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)

REQUESTABLE = [fmt for fmt in ConfigFormat if fmt is not ConfigFormat.block]


@pytest.fixture
def plain_user(access_token):
    core = create_core(access_token, name=unique_name("fmt_core"))
    group = create_group(access_token, name=unique_name("fmt_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("fmt_user")})
    yield user
    delete_user(access_token, user["username"])
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


@pytest.mark.parametrize("fmt", REQUESTABLE, ids=[fmt.value for fmt in REQUESTABLE])
def test_every_requestable_format_is_served_by_the_route_layer(plain_user, fmt):
    response = client.get(f"{plain_user['subscription_url']}/{fmt.value}")
    assert response.status_code not in (404, 405), f"{fmt.value}: route missing ({response.status_code})"
    assert response.status_code < 500, f"{fmt.value}: {response.status_code} {response.text[:200]}"


def test_l2tp_format_is_an_empty_list_for_a_user_without_l2tp_access(plain_user):
    response = client.get(f"{plain_user['subscription_url']}/l2tp")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert json.loads(response.text) == []


def test_html_subscription_page_renders_for_a_user_without_l2tp_access(plain_user):
    response = client.get(plain_user["subscription_url"], headers={"Accept": "text/html"})
    assert response.status_code == 200
    assert "{{" not in response.text
    assert 'id="l2tpModal"' not in response.text
    assert 'id="l2tpBtn"' not in response.text


@pytest.mark.parametrize("bogus", ["not-a-format", "L2TP", "openvpn.ovpn", "..", "l2tp%00"])
def test_an_unknown_format_is_a_clean_client_error(plain_user, bogus):
    response = client.get(f"{plain_user['subscription_url']}/{bogus}")
    assert 400 <= response.status_code < 500, f"{bogus!r}: {response.status_code} {response.text[:200]}"
