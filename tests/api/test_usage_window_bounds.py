from datetime import UTC, datetime, timedelta

import pytest

from tests.api import client
from tests.api.helpers import auth_headers, create_user, delete_user, unique_name

HUGE_WINDOW = {"start": "2000-01-01T00:00:00+00:00", "end": "2026-01-01T00:00:00+00:00", "period": "hour"}


@pytest.fixture
def operator_token(access_token):
    username = unique_name("operator")
    password = "Operator#12abCD"
    created = client.post(
        "/api/admin",
        headers=auth_headers(access_token),
        json={"username": username, "password": password, "role_id": 3},
    )
    assert created.status_code == 201, created.text
    token = client.post(
        "/api/admin/token", data={"username": username, "password": password, "grant_type": "password"}
    ).json()["access_token"]
    yield token
    client.delete(f"/api/admin/{username}", headers=auth_headers(access_token))


@pytest.mark.parametrize("path", ["/api/users/usage", "/api/users/counts/online"])
def test_operator_cannot_request_a_26_year_hourly_aggregation(operator_token, path):
    response = client.get(path, params=HUGE_WINDOW, headers=auth_headers(operator_token))
    assert response.status_code in (400, 422), f"{path} accepted a 26-year hourly window: {response.status_code}"


def test_subscription_token_cannot_request_a_26_year_hourly_aggregation(access_token):
    user = create_user(access_token, payload={"username": unique_name("usage_window")})
    try:
        response = client.get(f"{user['subscription_url']}/usage", params=HUGE_WINDOW)
        assert response.status_code in (400, 422), (
            f"/sub/.../usage accepted a 26-year hourly window: {response.status_code}"
        )
    finally:
        delete_user(access_token, user["username"])


ALL_TIME_BY_DAY = {"start": "2000-01-01T00:00:00+00:00", "end": "2026-09-24T00:00:00+00:00", "period": "day"}


@pytest.mark.parametrize("path", ["/api/users/usage", "/api/users/counts/online"])
def test_the_dashboards_all_time_chart_by_day_still_works(operator_token, path):
    response = client.get(path, params=ALL_TIME_BY_DAY, headers=auth_headers(operator_token))
    assert response.status_code == 200, response.text


def test_the_subscription_usage_serves_its_longest_daily_range_and_refuses_all_time(access_token):
    user = create_user(access_token, payload={"username": unique_name("usage_all_time")})
    end = datetime.now(UTC)
    longest_daily = {"start": (end - timedelta(days=400)).isoformat(), "end": end.isoformat(), "period": "day"}
    try:
        served = client.get(f"{user['subscription_url']}/usage", params=longest_daily)
        assert served.status_code == 200, served.text
        refused = client.get(f"{user['subscription_url']}/usage", params=ALL_TIME_BY_DAY)
        assert refused.status_code == 400, refused.text
    finally:
        delete_user(access_token, user["username"])
