from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_admin, delete_admin, unique_name


def _role(permissions: dict) -> dict:
    return {
        "name": unique_name("scoped_role"),
        "permissions": permissions,
        "limits": {},
        "features": {"can_use_reset_strategy": True, "can_use_next_plan": True},
        "access": {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": None},
        "hwid": {"mode": "use_global", "enabled": True, "forced": False},
    }


class ScopedAdmin:
    def __init__(self, access_token: str, permissions: dict):
        self.access_token = access_token
        created = client.post("/api/admin-role", headers=auth_headers(access_token), json=_role(permissions))
        assert created.status_code == status.HTTP_201_CREATED, created.text
        self.role_id = created.json()["id"]
        self.admin = create_admin(access_token, role_id=self.role_id)
        signed_in = client.post(
            "/api/admin/token",
            data={"username": self.admin["username"], "password": self.admin["password"], "grant_type": "password"},
        )
        assert signed_in.status_code == status.HTTP_200_OK, signed_in.text
        self.token = signed_in.json()["access_token"]

    def close(self) -> None:
        delete_admin(self.access_token, self.admin["username"])
        client.delete(f"/api/admin-role/{self.role_id}", headers=auth_headers(self.access_token))
