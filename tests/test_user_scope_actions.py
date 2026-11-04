import ast
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models.admin import AdminDetails, AdminRoleData
from app.operation import BaseOperation, OperatorType
from app.operation.hwid import HWIDOperation
from app.operation.permissions import PermissionDenied
from app.operation.user import UserOperation

OPERATION_DIR = pathlib.Path(__file__).resolve().parents[1] / "app" / "operation"
LOADERS = {"get_validated_user", "get_validated_user_by_id"}


def _admin(**user_actions) -> AdminDetails:
    return _admin_with_permissions({"users": {action: value for action, value in user_actions.items()}})


def _admin_with_permissions(permissions) -> AdminDetails:
    return AdminDetails(
        id=7,
        username="reseller",
        role=AdminRoleData(
            id=3,
            name="reseller",
            is_owner=False,
            permissions=permissions,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, action",
    [
        ("reset_user_data_usage_by_id", "reset_usage"),
        ("active_next_plan_by_id", "activate_next_plan"),
        ("set_owner_by_id", "set_owner"),
    ],
)
async def test_scoped_actions_use_their_own_scope_not_update(method, action):
    op = UserOperation(OperatorType.API)
    admin = _admin(read={"scope": 2}, update={"scope": 2}, **{action: {"scope": 1}})

    kwargs = {"db": AsyncMock(), "user_id": 1, "admin": admin}
    if method == "set_owner_by_id":
        kwargs["admin_username"] = "new-owner"

    with (
        patch("app.operation.get_admin", new_callable=AsyncMock) as _,
        patch("app.operation.get_user_by_id", new_callable=AsyncMock, return_value=None) as get_user_by_id,
        pytest.raises(HTTPException) as exc,
    ):
        await getattr(op, method)(**kwargs)

    assert exc.value.status_code == 404
    assert get_user_by_id.await_args.kwargs["admin_id"] == admin.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, action",
    [
        ("reset_user_data_usage_by_id", "reset_usage"),
        ("active_next_plan_by_id", "activate_next_plan"),
        ("set_owner_by_id", "set_owner"),
    ],
)
async def test_a_write_action_the_role_lacks_is_denied_even_with_update_all(method, action):
    op = UserOperation(OperatorType.API)
    admin = _admin(read={"scope": 2}, update={"scope": 2})

    kwargs = {"db": AsyncMock(), "user_id": 1, "admin": admin}
    if method == "set_owner_by_id":
        kwargs["admin_username"] = "new-owner"

    with (
        patch("app.operation.get_admin", new_callable=AsyncMock),
        patch("app.operation.get_user_by_id", new_callable=AsyncMock) as get_user_by_id,
        pytest.raises(PermissionDenied),
    ):
        await getattr(op, method)(**kwargs)

    get_user_by_id.assert_not_awaited()


FOREIGN_OWNER_ID = 99


def _user_owned_by(owner_id):
    def lookup(db, user_id, *, admin_id=None, **kwargs):
        if admin_id is not None and admin_id != owner_id:
            return None
        return SimpleNamespace(id=user_id)

    return lookup


def _lookup_admin_ids(get_user_by_id):
    return [call.kwargs["admin_id"] for call in get_user_by_id.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions",
    [
        pytest.param({"hwids": {"read": True}}, id="no-users-read"),
        pytest.param({"users": {"read": {"scope": 0}}, "hwids": {"read": True}}, id="users-read-disabled"),
    ],
)
async def test_hwid_reads_need_users_read_before_any_lookup(permissions):
    op = HWIDOperation(OperatorType.API)
    admin = _admin_with_permissions(permissions)

    with (
        patch(
            "app.operation.get_user_by_id", new_callable=AsyncMock, side_effect=_user_owned_by(admin.id)
        ) as get_user_by_id,
        patch("app.operation.hwid.get_user_hwids", new_callable=AsyncMock, return_value=[]) as read_hwids,
        pytest.raises(HTTPException) as exc,
    ):
        await op.get_user_hwids(AsyncMock(), 1, admin)

    assert (exc.value.status_code, exc.value.detail) == (404, "User not found")
    get_user_by_id.assert_not_awaited()
    read_hwids.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions",
    [
        pytest.param({"users": {"read": {"scope": 1}}, "hwids": {"read": True}}, id="users-read-own"),
        pytest.param({"users": {"read": True}, "hwids": {"read": {"scope": 1}}}, id="hwids-read-own"),
    ],
)
async def test_hwid_reads_apply_both_the_users_and_the_hwids_scope(permissions):
    op = HWIDOperation(OperatorType.API)
    admin = _admin_with_permissions(permissions)

    with (
        patch("app.operation.get_user_by_id", new_callable=AsyncMock, side_effect=_user_owned_by(FOREIGN_OWNER_ID)),
        patch("app.operation.hwid.get_user_hwids", new_callable=AsyncMock, return_value=[]) as read_hwids,
        pytest.raises(HTTPException) as exc,
    ):
        await op.get_user_hwids(AsyncMock(), 1, admin)

    assert (exc.value.status_code, exc.value.detail) == (404, "User not found")
    read_hwids.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "permissions",
    [
        pytest.param({"users": {"read": {"scope": 1}}}, id="hwids-read-missing"),
        pytest.param({"users": {"read": {"scope": 1}}, "hwids": {"read": {"scope": 0}}}, id="hwids-read-disabled"),
    ],
)
async def test_hwid_reads_without_the_hwid_permission_are_denied(permissions):
    op = HWIDOperation(OperatorType.API)
    admin = _admin_with_permissions(permissions)

    with (
        patch(
            "app.operation.get_user_by_id", new_callable=AsyncMock, side_effect=_user_owned_by(admin.id)
        ) as get_user_by_id,
        patch("app.operation.hwid.get_user_hwids", new_callable=AsyncMock, return_value=[]) as read_hwids,
        pytest.raises(PermissionDenied),
    ):
        await op.get_user_hwids(AsyncMock(), 1, admin)

    assert _lookup_admin_ids(get_user_by_id) == [admin.id]
    read_hwids.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, args, crud",
    [
        pytest.param("delete_user_hwid", ("hwid",), "delete_user_hwid", id="delete"),
        pytest.param("reset_user_hwids", (), "reset_user_hwids", id="reset"),
    ],
)
@pytest.mark.parametrize(
    "permissions, denial",
    [
        pytest.param({"hwids": {"delete": True}}, HTTPException, id="no-users-perms"),
        pytest.param(
            {"users": {"read": {"scope": 1}, "delete": True}, "hwids": {"delete": True}},
            HTTPException,
            id="users-read-own",
        ),
        pytest.param(
            {"users": {"read": True, "delete": {"scope": 1}}, "hwids": {"delete": True}},
            HTTPException,
            id="users-delete-own",
        ),
        pytest.param({"users": {"read": True}, "hwids": {"delete": True}}, PermissionDenied, id="users-delete-missing"),
        pytest.param(
            {"users": {"read": True, "delete": True}, "hwids": {"delete": {"scope": 1}}},
            HTTPException,
            id="hwids-delete-own",
        ),
    ],
)
async def test_hwid_deletes_follow_the_users_and_the_hwid_scope(method, args, crud, permissions, denial):
    op = HWIDOperation(OperatorType.API)
    admin = _admin_with_permissions(permissions)

    with (
        patch("app.operation.get_user_by_id", new_callable=AsyncMock, side_effect=_user_owned_by(FOREIGN_OWNER_ID)),
        patch(f"app.operation.hwid.{crud}", new_callable=AsyncMock, return_value=1) as write_hwids,
        patch("app.operation.hwid._clear_connection_state", new_callable=AsyncMock) as clear_state,
        pytest.raises(denial) as exc,
    ):
        await getattr(op, method)(AsyncMock(), 1, *args, admin)

    if denial is HTTPException:
        assert (exc.value.status_code, exc.value.detail) == (404, "User not found")
    write_hwids.assert_not_awaited()
    clear_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, args, crud, scope_lookups, expected",
    [
        pytest.param("get_user_hwids", (), "get_user_hwids", 2, None, id="read"),
        pytest.param("delete_user_hwid", ("hwid",), "delete_user_hwid", 3, {}, id="delete"),
        pytest.param("reset_user_hwids", (), "reset_user_hwids", 3, {"count": 0}, id="reset"),
    ],
)
@pytest.mark.parametrize(
    "grant, owner_id, scoped_admin_id",
    [
        pytest.param({"scope": 1}, 7, 7, id="own-scope-own-user"),
        pytest.param({"scope": 2}, FOREIGN_OWNER_ID, None, id="all-scope-foreign-user"),
        pytest.param(True, FOREIGN_OWNER_ID, None, id="unscoped-foreign-user"),
    ],
)
async def test_hwid_actions_reach_a_user_every_scope_allows(
    method, args, crud, scope_lookups, expected, grant, owner_id, scoped_admin_id
):
    op = HWIDOperation(OperatorType.API)
    admin = _admin_with_permissions(
        {"users": {"read": grant, "delete": grant}, "hwids": {"read": grant, "delete": grant}}
    )
    crud_result = [] if crud == "get_user_hwids" else (True if crud == "delete_user_hwid" else 0)

    with (
        patch(
            "app.operation.get_user_by_id", new_callable=AsyncMock, side_effect=_user_owned_by(owner_id)
        ) as get_user_by_id,
        patch(f"app.operation.hwid.{crud}", new_callable=AsyncMock, return_value=crud_result) as hwid_crud,
        patch("app.operation.hwid._clear_connection_state", new_callable=AsyncMock),
    ):
        result = await getattr(op, method)(AsyncMock(), 1, *args, admin)

    assert _lookup_admin_ids(get_user_by_id) == [scoped_admin_id] * scope_lookups
    hwid_crud.assert_awaited_once()
    if expected is None:
        assert (result.count, result.hwids) == (0, [])
    else:
        assert result == expected
