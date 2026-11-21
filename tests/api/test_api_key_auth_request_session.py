import asyncio

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import AsyncSession, get_db
from app.db.models import Admin, APIKey
from app.models.admin import AdminDetails
from app.routers import authentication
from tests.api import TestSession, client, get_test_db
from tests.api.helpers import auth_headers, create_admin, delete_admin, unique_name

API_KEY_AUTHENTICATION = (authentication.get_current, authentication.get_current_with_metrics)
WRITTEN_TELEGRAM_ID = 4242

write_then_fail_app = FastAPI()
write_then_fail_app.dependency_overrides[get_db] = get_test_db


@write_then_fail_app.post("/admins/{admin_id}/write-then-fail")
async def write_then_fail(
    admin_id: int,
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(authentication.get_current),
):
    db_admin = (await db.execute(select(Admin).where(Admin.id == admin_id))).scalar_one()
    db_admin.telegram_id = WRITTEN_TELEGRAM_ID
    await db.flush()
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="refused after a write")


write_then_fail_client = TestClient(write_then_fail_app)


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _stored(column, key_column, key_value):
    async def _get():
        async with TestSession() as session:
            return (await session.execute(select(column).where(key_column == key_value))).scalar_one_or_none()

    return asyncio.run(_get())


def _flatten_routes(routes):
    collected = []
    for route in routes:
        if isinstance(route, APIRoute):
            collected.append(route)
        elif type(route).__name__ == "_IncludedRouter" and hasattr(route, "original_router"):
            collected.extend(_flatten_routes(route.original_router.routes))
        elif hasattr(route, "routes"):
            collected.extend(_flatten_routes(route.routes))
    return collected


def _resolution_order(dependant):
    order = []
    for sub_dependant in dependant.dependencies:
        order.extend(_resolution_order(sub_dependant))
        order.append(sub_dependant)
    return order


def _receives_the_request_session(dependant) -> bool:
    return any(sub_dependant.call is get_db for sub_dependant in dependant.dependencies)


def test_a_request_that_fails_after_a_write_keeps_only_its_api_key_count(access_token):
    admin = create_admin(access_token, role_id=2)
    try:
        admin_token = _login(admin["username"], admin["password"])
        created = client.post(
            "/api/api_key",
            headers=auth_headers(admin_token),
            json={"name": unique_name("api_key")},
        )
        assert created.status_code == status.HTTP_201_CREATED
        raw_key, key_id = created.json()["api_key"], created.json()["id"]

        by_key = write_then_fail_client.post(f"/admins/{admin['id']}/write-then-fail", headers={"X-Api-Key": raw_key})
        assert by_key.status_code == status.HTTP_400_BAD_REQUEST
        assert _stored(Admin.telegram_id, Admin.id, admin["id"]) is None
        assert _stored(APIKey.request_count, APIKey.id, key_id) == 1

        by_session = write_then_fail_client.post(
            f"/admins/{admin['id']}/write-then-fail", headers=auth_headers(admin_token)
        )
        assert by_session.status_code == status.HTTP_400_BAD_REQUEST
        assert _stored(Admin.telegram_id, Admin.id, admin["id"]) is None
    finally:
        delete_admin(access_token, admin["username"])


def test_api_key_authentication_is_the_first_user_of_the_request_session_on_every_route():
    routes = _flatten_routes(client.app.routes)
    checked = 0
    session_users_before_authentication = []
    counted_twice = []
    for route in routes:
        order = _resolution_order(route.dependant)
        authentication_calls = {dependant.call for dependant in order if dependant.call in API_KEY_AUTHENTICATION}
        if not authentication_calls:
            continue
        checked += 1
        if len(authentication_calls) > 1:
            counted_twice.append((route.path, sorted(route.methods)))
        first = next(index for index, dependant in enumerate(order) if dependant.call in API_KEY_AUTHENTICATION)
        session_users_before_authentication.extend(
            (route.path, sorted(route.methods), getattr(dependant.call, "__qualname__", repr(dependant.call)))
            for dependant in order[:first]
            if _receives_the_request_session(dependant)
        )

    assert checked > 100
    assert session_users_before_authentication == []
    assert counted_twice == []
