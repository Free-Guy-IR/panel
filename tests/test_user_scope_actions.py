import ast
import pathlib
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models.admin import AdminDetails, AdminRoleData
from app.operation import BaseOperation, OperatorType
from app.operation.user import UserOperation

OPERATION_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "operation"
LOADERS = {"get_validated_user", "get_validated_user_by_id"}


def _admin(**user_actions) -> AdminDetails:
    return AdminDetails(
        id=7,
        username="reseller",
        role=AdminRoleData(
            id=3,
            name="reseller",
            is_owner=False,
            permissions={"users": {action: value for action, value in user_actions.items()}},
        ),
    )


def test_every_loader_call_names_the_action_it_performs():
    offenders = []
    for path in sorted(OPERATION_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in LOADERS:
                continue
            if not any(keyword.arg == "scope_action" for keyword in node.keywords):
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], f"loader calls without an explicit scope_action: {offenders}"


def test_the_loader_refuses_to_be_called_without_an_action():
    op = BaseOperation(OperatorType.API)

    with pytest.raises(TypeError):
        op.get_validated_user(AsyncMock(), "someone", _admin(read=True))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, action",
    [("remove_user", "delete"), ("modify_user", "update"), ("revoke_user_sub", "revoke_sub")],
)
async def test_a_by_username_write_is_confined_to_the_admins_own_users(method, action):
    op = UserOperation(OperatorType.API)
    admin = _admin(read={"scope": 2}, **{action: {"scope": 1}})

    with (
        patch("app.operation.get_user", new_callable=AsyncMock, return_value=None) as get_user,
        pytest.raises(HTTPException) as exc,
    ):
        await op.get_validated_user(AsyncMock(), "someone-elses-user", admin, scope_action=action)

    assert exc.value.status_code == 404
    assert get_user.await_args.kwargs["admin_id"] == admin.id


@pytest.mark.asyncio
async def test_a_by_username_read_stays_on_the_read_scope():
    op = BaseOperation(OperatorType.API)
    admin = _admin(read={"scope": 1}, delete={"scope": 2})

    with (
        patch("app.operation.get_user", new_callable=AsyncMock, return_value=None) as get_user,
        pytest.raises(HTTPException),
    ):
        await op.get_validated_user(AsyncMock(), "someone", admin, scope_action="read")

    assert get_user.await_args.kwargs["admin_id"] == admin.id


def test_the_two_list_endpoints_read_their_own_scope():
    source = (OPERATION_DIR / "user.py").read_text()
    tree = ast.parse(source)

    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name not in ("get_users", "get_users_simple"):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "get_scope_admin_id"
                and len(call.args) == 3
            ):
                found[node.name] = ast.literal_eval(call.args[2])

    assert found == {"get_users": "read", "get_users_simple": "read_simple"}
