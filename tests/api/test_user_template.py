from tests.api import client
from fastapi import status
from tests.api.test_admin import test_admin_login


def test_user_template_create():
    """Test that the user template create route is accessible."""
    access_token = test_admin_login()
    response = client.post(
        "/api/user_template",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "name": "test_user_template",
            "group_ids": [2],
            "data_limit": (1024 * 1024 * 1024),
            "expire_duration": 3600,
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["name"] == "test_user_template"
    assert response.json()["group_ids"] == [2]
    assert response.json()["data_limit"] == (1024 * 1024 * 1024)
    assert response.json()["expire_duration"] == 3600


def test_user_template_get():
    """Test that the user template get route is accessible."""
    access_token = test_admin_login()
    response = client.get(
        "/api/user_template",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()) > 0


def test_user_template_update():
    """Test that the user template update route is accessible."""
    access_token = test_admin_login()
    response = client.put(
        "/api/user_template/1",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "name": "test_user_template_updated",
            "group_ids": [2, 3],
            "expire_duration": (86400 * 30),
        },
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "test_user_template_updated"
    assert response.json()["group_ids"] == [2, 3]
    assert response.json()["expire_duration"] == (86400 * 30)


def test_user_template_get_by_id():
    """Test that the user template get by id route is accessible."""
    access_token = test_admin_login()
    response = client.get(
        "/api/user_template/1",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["id"] == 1
    assert response.json()["name"] == "test_user_template_updated"
    assert response.json()["group_ids"] == [2, 3]
    assert response.json()["expire_duration"] == (86400 * 30)


def test_user_template_delete():
    """Test that the user template delete route is accessible."""
    access_token = test_admin_login()
    response = client.delete(
        "/api/user_template/1",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
