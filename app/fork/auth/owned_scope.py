from app.models.admin_role import PermissionScope

OWNED_RESOURCES = frozenset({"users", "api_keys", "hwids"})


def scope_own_refusal(resource: str, action: str, scope: PermissionScope | None) -> str | None:
    if scope is PermissionScope.OWN and resource not in OWNED_RESOURCES:
        return f"Permission denied: {resource}.{action} (scope own applies only to users, api_keys and hwids)"
    return None
