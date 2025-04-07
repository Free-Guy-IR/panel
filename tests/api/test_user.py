from datetime import timedelta, datetime, timezone
from tests.api import client
from fastapi import status
from tests.api.test_admin import test_admin_login


def test_user_create_active():
    """Test that the user create active route is accessible."""
    access_token = test_admin_login()
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)
    response = client.post(
        "/api/user",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "username": "test_user_active",
            "proxy_settings": {},
            "group_ids": [2, 3],
            "expire": expire.isoformat(),
            "data_limit": (1024 * 1024 * 1024 * 10),
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["username"] == "test_user_active"
    assert response.json()["data_limit"] == (1024 * 1024 * 1024 * 10)
    assert response.json()["data_limit_reset_strategy"] == "no_reset"
    assert response.json()["status"] == "active"

    for group in response.json()["group_ids"]:
        assert group in [2, 3]
    
    # Parse the response date string back to datetime
    response_datetime = datetime.fromisoformat(response.json()["expire"])
    # Format both to the same format without microseconds
    expected_formatted = expire.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
    response_formatted = response_datetime.strftime("%Y-%m-%dT%H:%M:%S")
    
    assert response_formatted == expected_formatted


def test_user_create_on_hold():
    """Test that the user create on hold route is accessible."""
    access_token = test_admin_login()
    expire = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)
    response = client.post(
        "/api/user",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "username": "test_user_on_hold",
            "proxy_settings": {},
            "group_ids": [2, 3],
            "data_limit": (1024 * 1024 * 1024 * 10),
            "data_limit_reset_strategy": "no_reset",
            "status": "on_hold",
            "on_hold_timeout": expire.isoformat(),
            "on_hold_expire_duration": (86400 * 30),
        },
    )
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["username"] == "test_user_on_hold"
    assert response.json()["data_limit"] == (1024 * 1024 * 1024 * 10)
    assert response.json()["data_limit_reset_strategy"] == "no_reset"
    assert response.json()["status"] == "on_hold"
    assert response.json()["on_hold_expire_duration"] == (86400 * 30)

    for group in response.json()["group_ids"]:
        assert group in [2, 3]

    # Parse the response date string back to datetime
    response_datetime = datetime.fromisoformat(response.json()["on_hold_timeout"])
    # Format both to the same format without microseconds
    expected_formatted = expire.replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
    response_formatted = response_datetime.strftime("%Y-%m-%dT%H:%M:%S")
    
    assert response_formatted == expected_formatted


def test_users_get():
    """Test that the user get route is accessible."""
    access_token = test_admin_login()
    response = client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()["users"]) > 0


def test_user_get():
    """Test that the user get by id route is accessible."""
    access_token = test_admin_login()
    response = client.get(
        "/api/users?username=test_user_active",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert len(response.json()["users"]) == 1
    assert response.json()["users"][0]["username"] == "test_user_active"


def test_user_update():
    """Test that the user update route is accessible."""
    access_token = test_admin_login()
    response = client.put(
        "/api/user/test_user_active",
        headers={"Authorization": f"Bearer {access_token}"},
        json={
            "group_ids": [3],
            "data_limit": (1024 * 1024 * 1024 * 10),
        },
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["username"] == "test_user_active"
    assert response.json()["group_ids"] == [3]
    assert response.json()["data_limit"] == (1024 * 1024 * 1024 * 10)


def test_user_delete():
    """Test that the user delete route is accessible."""
    access_token = test_admin_login()
    response = client.delete(
        "/api/user/test_user_active",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
