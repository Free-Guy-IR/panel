import asyncio

from fastapi import status

from app.db.models import Group
from tests.api import TestSession, client


async def _create_group_record(name: str) -> int:
    async with TestSession() as session:
        group = Group(name=name, inbounds=[], is_disabled=False)
        session.add(group)
        await session.commit()
        await session.refresh(group)
        return group.id


async def _delete_group_record(group_id: int):
    async with TestSession() as session:
        group = await session.get(Group, group_id)
        if group:
            await session.delete(group)
            await session.commit()


def test_admin_login():
    """Test that the admin login route is accessible."""

    response = client.post(
        url="/api/admin/token",
        data={"username": "testadmin", "password": "testadmin", "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert "access_token" in response.json()
    return response.json()["access_token"]


def test_get_admin(access_token):
    """Test that the admin get route is accessible."""

    # mock_settings(monkeypatch)
    username = "testadmin"
    response = client.get(
        url="/api/admin",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["username"] == username


def test_admin_create(access_token):
    """Test that the admin create route is accessible."""

    username = "testadmincreate"
    password = "TestAdmincreate#11"
    response = client.post(
        url="/api/admin",
        json={"username": username, "password": password, "is_sudo": False},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["username"] == username


def test_admin_db_login():
    """Test that the admin db login route is accessible."""

    username = "testadmincreate"
    password = "TestAdmincreate#11"
    response = client.post(
        url="/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert "access_token" in response.json()


def test_update_admin(access_token):
    """Test that the admin update route is accessible."""

    username = "testadmincreate"
    password = "TestAdminupdate#11"
    response = client.put(
        url=f"/api/admin/{username}",
        json={
            "password": password,
            "is_sudo": False,
            "is_disabled": True,
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["username"] == username
    assert response.json()["is_sudo"] is False
    assert response.json()["is_disabled"] is True


def test_get_admins(access_token):
    """Test that the admins get route is accessible."""

    username = "testadmincreate"
    response = client.get(
        url="/api/admins",
        params={"sort": "-created_at"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert username in [admin["username"] for admin in response.json()]


def test_disable_admin():
    """Test that the admin disable route is accessible."""

    username = "testadmincreate"
    password = "TestAdminupdate#11"
    response = client.post(
        url="/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "your account has been disabled"


def test_admin_delete_all_users_endpoint(access_token):
    """Test deleting all users belonging to an admin."""

    admin_username = "testadminbulkdelete"
    admin_password = "TestAdminBulkdelete#11"

    response = client.post(
        url="/api/admin",
        json={"username": admin_username, "password": admin_password, "is_sudo": False},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_201_CREATED

    group_id = asyncio.run(_create_group_record(f"{admin_username}_group"))

    created_users = []
    for idx in range(2):
        user_name = f"{admin_username}_user_{idx}"
        user_response = client.post(
            "/api/user",
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "username": user_name,
                "proxy_settings": {},
                "group_ids": [group_id],
                "data_limit": 1024,
                "data_limit_reset_strategy": "no_reset",
                "status": "active",
            },
        )
        assert user_response.status_code == status.HTTP_201_CREATED
        created_users.append(user_name)

        ownership_response = client.put(
            f"/api/user/{user_name}/set_owner",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"admin_username": admin_username},
        )
        assert ownership_response.status_code == status.HTTP_200_OK
        assert ownership_response.json()["admin"]["username"] == admin_username

    response = client.delete(
        url=f"/api/admin/{admin_username}/users",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["deleted"] == len(created_users)

    for username in created_users:
        user_check = client.get(
            "/api/users",
            params={"username": username},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        assert user_check.status_code == status.HTTP_200_OK
        assert user_check.json()["users"] == []

    cleanup = client.delete(
        url=f"/api/admin/{admin_username}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert cleanup.status_code == status.HTTP_204_NO_CONTENT

    asyncio.run(_delete_group_record(group_id))


def test_admin_delete(access_token):
    """Test that the admin delete route is accessible."""

    username = "testadmincreate"
    response = client.delete(
        url=f"/api/admin/{username}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
