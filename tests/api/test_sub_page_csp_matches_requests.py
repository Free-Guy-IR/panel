import re
from pathlib import Path

import pytest

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


@pytest.fixture
def page_user(access_token):
    core = create_core(access_token, name=unique_name("csp_core"))
    group = create_group(access_token, name=unique_name("csp_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("csp_user")})
    yield user
    delete_user(access_token, user["username"])
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


def _directive(csp: str, name: str) -> str:
    for part in csp.split(";"):
        part = part.strip()
        if part.startswith(name + " "):
            return part[len(name) + 1 :]
    return ""


def test_the_page_policy_allows_the_requests_the_page_makes(page_user):
    response = client.get(page_user["subscription_url"], headers={"Accept": "text/html"})
    assert response.status_code == 200
    template = Path("app/templates/subscription/index.html").read_text()
    assert re.search(r"await fetch\(\s*window\.location\.pathname", template)
    connect_src = _directive(response.headers["content-security-policy"], "connect-src")
    assert "'self'" in connect_src, f"page fetches its own /openvpn route but connect-src is {connect_src!r}"


def test_the_page_policy_still_forbids_cross_origin_requests(page_user):
    response = client.get(page_user["subscription_url"], headers={"Accept": "text/html"})
    policy = response.headers["content-security-policy"]
    assert _directive(policy, "connect-src") == "'self'"
    assert _directive(policy, "default-src") == "'none'"
    assert _directive(policy, "form-action") == "'none'"
