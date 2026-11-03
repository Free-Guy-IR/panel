import pytest

from app.models.admin import AdminDetails, AdminRoleData
from app.models.admin_role import RolePermissions
from app.operation.node import NodeOperation
from app.operation.permissions import intersect_permissions

OWN = {"scope": 1}
ALL = {"scope": 2}


def _admin(permissions: dict, *, is_owner: bool = False) -> AdminDetails:
    return AdminDetails(
        id=7,
        username="count_scope_admin",
        role=AdminRoleData(id=9, name="r", is_owner=is_owner, permissions=RolePermissions.model_validate(permissions)),
    )


@pytest.mark.parametrize(
    "users_read",
    [
        pytest.param(None, id="missing"),
        pytest.param(False, id="false"),
        pytest.param({"scope": 0}, id="scope-none"),
        pytest.param(OWN, id="scope-own"),
    ],
)
def test_user_counts_stay_on_own_users_without_users_read_all(users_read):
    permissions = {"nodes": {"stats": True}}
    if users_read is not None:
        permissions["users"] = {"read": users_read}

    admins, _ = NodeOperation._user_count_scope(_admin(permissions))

    assert admins == ["count_scope_admin"]


@pytest.mark.parametrize("users_read", [pytest.param(True, id="true"), pytest.param(ALL, id="scope-all")])
def test_user_counts_cover_all_users_with_users_read_all(users_read):
    admins, _ = NodeOperation._user_count_scope(_admin({"nodes": {"stats": True}, "users": {"read": users_read}}))

    assert admins is None


def test_owner_user_counts_cover_all_users():
    assert NodeOperation._user_count_scope(_admin({}, is_owner=True)) == (None, None)


def test_narrowing_the_role_never_widens_a_key_user_count_scope():
    key = RolePermissions.model_validate({"users": {"read": OWN}, "nodes": {"stats": True}})
    wide_role = RolePermissions.model_validate({"users": {"read": OWN}, "nodes": {"stats": True}})
    narrow_role = RolePermissions.model_validate({"nodes": {"stats": True}})

    before, _ = NodeOperation._user_count_scope(_admin(intersect_permissions(key, wide_role).model_dump()))
    after, _ = NodeOperation._user_count_scope(_admin(intersect_permissions(key, narrow_role).model_dump()))

    assert before == ["count_scope_admin"]
    assert after == ["count_scope_admin"]
