from fastapi import status

from tests.api import client
from tests.api.helpers import create_core, delete_core, create_group, delete_group, get_inbounds, unique_name


def test_group_create(access_token):
    """Test that the group create route is accessible."""

    core = create_core(access_token)
    inbounds = get_inbounds(access_token)
    assert inbounds, "Expected at least one inbound tag"
    created_groups = []
    try:
        for _ in range(3):
            selected_inbounds = inbounds[: min(3, len(inbounds))]
            response = create_group(
                access_token, name=unique_name("testgroup"), inbound_tags=selected_inbounds
            )
            created_groups.append(response["id"])
            assert response["name"].startswith("testgroup")
            assert response["inbound_tags"] == selected_inbounds
    finally:
        for group_id in created_groups:
            delete_group(access_token, group_id)
        delete_core(access_token, core["id"])


def test_group_update(access_token):
    """Test that the group update route is accessible."""

    core = create_core(access_token)
    group = create_group(access_token)
    response = client.put(
        url=f"/api/group/{group['id']}",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"name": "testgroup4", "is_disabled": True},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "testgroup4"
    assert response.json()["is_disabled"] is True
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


def test_group_delete(access_token):
    """Test that the group delete route is accessible."""

    core = create_core(access_token)
    group = create_group(access_token)
    response = client.delete(
        url=f"/api/group/{group['id']}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_204_NO_CONTENT
    delete_core(access_token, core["id"])


def test_group_get_by_id(access_token):
    """Test that the group get by id route is accessible."""

    core = create_core(access_token)
    group = create_group(access_token, name="testgroup_lookup")
    response = client.get(
        url=f"/api/group/{group['id']}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "testgroup_lookup"
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


def test_groups_get(access_token):
    """Test that the group get route is accessible."""

    core = create_core(access_token)
    group_one = create_group(access_token, name="testgroup_total_1")
    group_two = create_group(access_token, name="testgroup_total_2")
    response = client.get(
        url="/api/groups",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert response.status_code == status.HTTP_200_OK
    names = [group["name"] for group in response.json()["groups"]]
    assert "testgroup_total_1" in names
    assert "testgroup_total_2" in names
    delete_group(access_token, group_one["id"])
    delete_group(access_token, group_two["id"])
    delete_core(access_token, core["id"])
