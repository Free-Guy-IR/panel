import asyncio
import json
import random

import pytest
from fastapi import status
from sqlalchemy import update

from app.db.models import ClientTemplate
from tests.api import GetTestDB, client
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

LINE_BREAKS = [
    pytest.param(
        {"route": {"rules": [{"domain": ["\nexample.com"], "domain_suffix": ["example.com"], "outbound": "direct"}]}},
        "route.rules[0].domain[0]",
        id="route-newline-collides-with-a-suffix",
    ),
    pytest.param(
        {"route": {"rules": [{"domain": "\r.example.com", "domain_suffix": [".example.com"], "outbound": "direct"}]}},
        "route.rules[0].domain[0]",
        id="route-carriage-return-string-collides-with-a-dot-suffix",
    ),
    pytest.param(
        {
            "dns": {
                "servers": [{"type": "local", "tag": "dns-local"}],
                "rules": [{"domain": ["\nexample.com"], "domain_suffix": ["example.com"], "server": "dns-local"}],
            }
        },
        "dns.rules[0].domain[0]",
        id="dns-rule",
    ),
    pytest.param(
        {
            "route": {
                "rules": [
                    {
                        "type": "logical",
                        "mode": "or",
                        "rules": [
                            {"domain": ["ok.com"]},
                            {"domain": ["\nexample.com"], "domain_suffix": ["example.com"]},
                        ],
                        "outbound": "direct",
                    }
                ]
            }
        },
        "route.rules[0].rules[1].domain[0]",
        id="nested-logical-rule",
    ),
    pytest.param(
        {
            "route": {
                "rule_set": [
                    {"type": "inline", "tag": "ads", "rules": [{"domain": ["\nads.com"], "domain_suffix": ["ads.com"]}]}
                ],
                "rules": [{"rule_set": ["ads"], "outbound": "direct"}],
            }
        },
        "route.rule_set[0].rules[0].domain[0]",
        id="inline-rule-set",
    ),
    pytest.param(
        {"route": {"rules": [{"domain": ["\r"], "outbound": "direct"}]}},
        "route.rules[0].domain[0]",
        id="carriage-return-alone-matches-every-name",
    ),
    pytest.param(
        {"route": {"rules": [{"domain_suffix": ["example.com", "evil\n.com"], "outbound": "direct"}]}},
        "route.rules[0].domain_suffix[1]",
        id="suffix-list",
    ),
    pytest.param(
        {"route": {"rules": [{"domain_suffix": ["example.org", "example.com\r"], "outbound": "direct"}]}},
        "route.rules[0].domain_suffix[1]",
        id="crlf-pasted-suffix-matches-every-name",
    ),
    pytest.param(
        {
            "route": {
                "rule_set": [{"tag": "ads", "rules": [{"domain": ["\nads.com"], "domain_suffix": ["ads.com"]}]}],
                "rules": [{"rule_set": ["ads"], "outbound": "direct"}],
            }
        },
        "route.rule_set[0].rules[0].domain[0]",
        id="inline-rule-set-without-a-type",
    ),
    pytest.param(
        {
            "dns": {
                "servers": [{"type": "local", "tag": "dns-local"}],
                "rules": [
                    {
                        "type": "logical",
                        "mode": "or",
                        "rules": [{"domain": ["\nexample.com"], "domain_suffix": ["example.com"]}],
                        "server": "dns-local",
                    }
                ],
            }
        },
        "dns.rules[0].rules[0].domain[0]",
        id="dns-logical-rule",
    ),
    pytest.param(
        {"Route": {"Rules": [{"domain": ["\nexample.com"], "domain_suffix": ["example.com"], "outbound": "direct"}]}},
        "Route.Rules[0].domain[0]",
        id="container-keys-in-another-case",
    ),
    pytest.param(
        {
            "dnſ": {
                "servers": [{"type": "local", "tag": "dns-local"}],
                "rules": [{"domain": ["\nexample.com"], "domain_suffix": ["example.com"], "server": "dns-local"}],
            }
        },
        "dnſ.rules[0].domain[0]",
        id="container-key-with-a-long-s",
    ),
    pytest.param(
        {
            "route": {"rules": []},
            "Route": {"rules": [{"domain": ["\nexample.com"], "domain_suffix": ["example.com"], "outbound": "direct"}]},
        },
        "Route.rules[0].domain[0]",
        id="second-route-key-in-another-case",
    ),
]

UNMATCHED_FIELDS = {
    "route": {
        "rules": [
            {"domain_keyword": ["ev\nil"], "outbound": "direct"},
            {"domain_regex": ["^evil\n$"], "outbound": "direct"},
        ]
    }
}

CLEAN = {
    "route": {
        "rule_set": [{"type": "inline", "tag": "ads", "rules": [{"domain_suffix": ["ads.com"]}]}],
        "rules": [
            {"domain": ["example.com"], "domain_suffix": ["example.com"], "outbound": "direct"},
            {"type": "logical", "mode": "and", "rules": [{"domain_regex": ["^a\\.b$"]}], "outbound": "direct"},
            {"rule_set": ["ads"], "outbound": "direct"},
        ],
    }
}

TEMPLATE_BASE = {
    "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": 2080}],
    "outbounds": [
        {"type": "selector", "tag": "proxy", "outbounds": None},
        {"type": "direct", "tag": "direct"},
    ],
}


def _singbox_core_config(sections: dict) -> dict:
    return {
        "log": {"level": "error"},
        "inbounds": [
            {
                "type": "hysteria2",
                "tag": unique_name("lb_hy2"),
                "listen": "127.0.0.1",
                "listen_port": random.randint(20000, 60000),
                "users": [],
                "tls": {
                    "enabled": True,
                    "alpn": ["h3"],
                    "certificate_path": "/etc/ssl/certs/hy2.crt",
                    "key_path": "/etc/ssl/private/hy2.key",
                },
            }
        ],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        **sections,
    }


def _post_core(access_token: str, name: str, sections: dict):
    return client.post(
        "/api/core",
        headers=auth_headers(access_token),
        json={
            "name": name,
            "config": _singbox_core_config(sections),
            "type": "singbox",
            "exclude_inbound_tags": [],
            "fallbacks_inbound_tags": [],
        },
    )


def _post_template(access_token: str, content: str, is_default: bool = False):
    return client.post(
        "/api/client_template",
        headers=auth_headers(access_token),
        json={
            "name": unique_name("lb_tmpl"),
            "template_type": "singbox_subscription",
            "content": content,
            "is_default": is_default,
        },
    )


def _core_names(access_token: str) -> set[str]:
    response = client.get("/api/cores", headers=auth_headers(access_token), params={"limit": 1000})
    assert response.status_code == status.HTTP_200_OK, response.text
    return {core["name"] for core in response.json()["cores"]}


def _default_singbox_template(access_token: str) -> dict:
    response = client.get(
        "/api/client_templates",
        headers=auth_headers(access_token),
        params={"template_type": "singbox_subscription"},
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return next(template for template in response.json()["templates"] if template["is_default"])


def _make_default(access_token: str, template_id: int) -> None:
    response = client.put(
        f"/api/client_template/{template_id}", headers=auth_headers(access_token), json={"is_default": True}
    )
    assert response.status_code == status.HTTP_200_OK, response.text


async def _store_template_content(template_id: int, content: str) -> None:
    async with GetTestDB() as db:
        await db.execute(update(ClientTemplate).where(ClientTemplate.id == template_id).values(content=content))
        await db.commit()


def _domain_values_with_line_breaks(rules, path: str) -> list[str]:
    found = []
    for index, rule in enumerate(rules or []):
        for field in ("domain", "domain_suffix"):
            values = rule.get(field)
            values = [values] if isinstance(values, str) else values or []
            found += [f"{path}[{index}].{field}" for value in values if "\n" in value or "\r" in value]
        found += _domain_values_with_line_breaks(rule.get("rules") if rule.get("type") == "logical" else [], path)
    return found


@pytest.mark.parametrize(("sections", "path"), LINE_BREAKS)
def test_a_singbox_core_with_a_line_break_in_a_domain_value_is_refused(access_token, sections, path):
    name = unique_name("lb_core")
    response = _post_core(access_token, name, sections)
    try:
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
        assert path in response.json()["detail"]
        assert name not in _core_names(access_token)
    finally:
        if response.status_code == status.HTTP_201_CREATED:
            delete_core(access_token, response.json()["id"])


def test_a_singbox_core_cannot_be_modified_to_carry_a_line_break(access_token):
    created = _post_core(access_token, unique_name("lb_core"), CLEAN)
    assert created.status_code == status.HTTP_201_CREATED, created.text
    core = created.json()
    sections, path = LINE_BREAKS[0].values
    try:
        response = client.put(
            f"/api/core/{core['id']}",
            headers=auth_headers(access_token),
            params={"restart_nodes": False},
            json={
                "name": core["name"],
                "type": "singbox",
                "config": {**core["config"], **sections},
                "exclude_inbound_tags": [],
                "fallbacks_inbound_tags": [],
            },
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
        assert path in response.json()["detail"]
        stored = client.get(f"/api/core/{core['id']}", headers=auth_headers(access_token)).json()
        assert stored["config"]["route"] == core["config"]["route"]
    finally:
        delete_core(access_token, core["id"])


@pytest.mark.parametrize(("sections", "path"), LINE_BREAKS)
def test_a_singbox_subscription_template_with_a_line_break_in_a_domain_value_is_refused(access_token, sections, path):
    response = _post_template(access_token, json.dumps(TEMPLATE_BASE | sections))
    try:
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
        assert path in response.json()["detail"]
    finally:
        if response.status_code == status.HTTP_201_CREATED:
            client.delete(f"/api/client_template/{response.json()['id']}", headers=auth_headers(access_token))


def test_a_line_break_the_template_check_render_would_hide_is_still_refused(access_token):
    hidden = {
        "route": {
            "rules": [{"domain": ["{#", "\nexample.com", "#}"], "domain_suffix": ["example.com"], "outbound": "direct"}]
        }
    }
    response = _post_template(access_token, json.dumps(TEMPLATE_BASE | hidden))
    try:
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
        assert "route.rules[0].domain[1]" in response.json()["detail"]
    finally:
        if response.status_code == status.HTTP_201_CREATED:
            client.delete(f"/api/client_template/{response.json()['id']}", headers=auth_headers(access_token))


def test_a_refused_line_break_template_never_becomes_the_served_sing_box_template(access_token):
    core = create_core(access_token, name=unique_name("lb_sub_core"))
    hosts = create_hosts_for_inbounds(access_token)
    group = create_group(access_token, name=unique_name("lb_sub_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("lb_sub_user")})
    previous_default = _default_singbox_template(access_token)
    sections, _ = LINE_BREAKS[0].values
    response = _post_template(access_token, json.dumps(TEMPLATE_BASE | sections), is_default=True)
    try:
        served = client.get(f"{user['subscription_url']}/sing_box")
        assert served.status_code == status.HTTP_200_OK, served.text
        config = json.loads(served.text)
        route = config.get("route") or {}
        offenders = _domain_values_with_line_breaks(route.get("rules"), "route.rules")
        offenders += _domain_values_with_line_breaks((config.get("dns") or {}).get("rules"), "dns.rules")
        assert offenders == []
    finally:
        _make_default(access_token, previous_default["id"])
        if response.status_code == status.HTTP_201_CREATED:
            client.delete(f"/api/client_template/{response.json()['id']}", headers=auth_headers(access_token))
        delete_user(access_token, user["username"])
        for host in hosts:
            client.delete(f"/api/host/{host['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_a_line_break_template_stored_before_the_check_is_served_as_stored(access_token):
    core = create_core(access_token, name=unique_name("lb_old_core"))
    hosts = create_hosts_for_inbounds(access_token)
    group = create_group(access_token, name=unique_name("lb_old_group"))
    user = create_user(access_token, group_ids=[group["id"]], payload={"username": unique_name("lb_old_user")})
    previous_default = _default_singbox_template(access_token)
    created = _post_template(access_token, json.dumps(TEMPLATE_BASE | CLEAN))
    assert created.status_code == status.HTTP_201_CREATED, created.text
    sections, _ = LINE_BREAKS[0].values
    try:
        asyncio.run(_store_template_content(created.json()["id"], json.dumps(TEMPLATE_BASE | sections)))
        _make_default(access_token, created.json()["id"])
        served = client.get(f"{user['subscription_url']}/sing_box")
        assert served.status_code == status.HTTP_200_OK, served.text
        rules = json.loads(served.text)["route"]["rules"]
        assert _domain_values_with_line_breaks(rules, "route.rules") == ["route.rules[0].domain"]
    finally:
        _make_default(access_token, previous_default["id"])
        client.delete(f"/api/client_template/{created.json()['id']}", headers=auth_headers(access_token))
        delete_user(access_token, user["username"])
        for host in hosts:
            client.delete(f"/api/host/{host['id']}", headers=auth_headers(access_token))
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


@pytest.mark.parametrize("sections", [CLEAN, UNMATCHED_FIELDS], ids=["clean", "keyword-and-regex"])
def test_rules_sing_box_loads_still_save(access_token, sections):
    core = _post_core(access_token, unique_name("lb_core"), sections)
    template = _post_template(access_token, json.dumps(TEMPLATE_BASE | sections))
    try:
        assert core.status_code == status.HTTP_201_CREATED, core.text
        assert template.status_code == status.HTTP_201_CREATED, template.text
    finally:
        if core.status_code == status.HTTP_201_CREATED:
            delete_core(access_token, core.json()["id"])
        if template.status_code == status.HTTP_201_CREATED:
            client.delete(f"/api/client_template/{template.json()['id']}", headers=auth_headers(access_token))
