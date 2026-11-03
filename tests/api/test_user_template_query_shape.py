from contextlib import contextmanager

from fastapi import status
from sqlalchemy import event

from tests.api import client, engine
from tests.api.helpers import (
    auth_headers,
    create_core,
    create_group,
    create_user,
    create_user_template,
    delete_core,
    delete_group,
    delete_user,
    delete_user_template,
    unique_name,
)

GROUP_MEMBER_TABLE = "users_groups_association"
GROUP_INBOUND_TABLE = "inbounds_groups_association"


@contextmanager
def captured_statements():
    statements: list[str] = []

    def _record(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


def _member_loads(statements: list[str]) -> list[str]:
    return [stmt for stmt in statements if GROUP_MEMBER_TABLE in stmt]


def _inbound_loads(statements: list[str]) -> list[str]:
    return [stmt for stmt in statements if stmt.lstrip().startswith("select") and GROUP_INBOUND_TABLE in stmt]


def _template_group_selects(statements: list[str]) -> list[str]:
    return [stmt for stmt in statements if stmt.lstrip().startswith("select") and "template_group_association" in stmt]


def test_create_and_modify_template_do_not_load_group_members(access_token):
    core = create_core(access_token)
    groups = [create_group(access_token, name=unique_name("tpl_shape_group")) for _ in range(2)]
    member = create_user(access_token, username=unique_name("tpl_shape_member"), group_ids=[g["id"] for g in groups])
    template = None
    try:
        with captured_statements() as create_statements:
            template = create_user_template(access_token, group_ids=[groups[0]["id"]])
        assert template["group_ids"] == [groups[0]["id"]]
        assert _member_loads(create_statements) == []
        assert len(_inbound_loads(create_statements)) <= 1

        with captured_statements() as modify_statements:
            response = client.put(
                f"/api/user_template/{template['id']}",
                headers=auth_headers(access_token),
                json={"group_ids": [groups[1]["id"]]},
            )
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["group_ids"] == [groups[1]["id"]]
        assert _member_loads(modify_statements) == []
        assert len(_inbound_loads(modify_statements)) <= 1
    finally:
        if template is not None:
            delete_user_template(access_token, template["id"])
        delete_user(access_token, member["username"])
        for group in groups:
            delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def test_template_list_loads_groups_in_one_batched_query(access_token):
    core = create_core(access_token)
    groups = [create_group(access_token, name=unique_name("tpl_list_group")) for _ in range(2)]
    templates = [
        create_user_template(access_token, group_ids=[groups[idx % 2]["id"]], name=unique_name(f"tpl_list_{idx}"))
        for idx in range(4)
    ]
    try:
        with captured_statements() as statements:
            response = client.get(
                "/api/user_templates",
                headers=auth_headers(access_token),
                params=[("ids", template["id"]) for template in templates],
            )
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert [item["id"] for item in body] == [template["id"] for template in templates]
        assert [item["group_ids"] for item in body] == [[groups[idx % 2]["id"]] for idx in range(4)]
        assert len(_template_group_selects(statements)) == 1
        assert _member_loads(statements) == []
    finally:
        for template in templates:
            delete_user_template(access_token, template["id"])
        for group in groups:
            delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])
