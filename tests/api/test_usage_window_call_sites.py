from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_admin, create_user, delete_admin, delete_user, unique_name

TWENTY_SIX_YEARS = timedelta(days=26 * 365)


def _window(span: timedelta, period: str) -> dict[str, str]:
    end = datetime.now(UTC)
    return {"start": (end - span).isoformat(), "end": end.isoformat(), "period": period}


def _all_time() -> dict[str, str]:
    end = datetime.now(UTC)
    return {"start": datetime(2000, 1, 1, tzinfo=UTC).isoformat(), "end": end.isoformat(), "period": "day"}


@pytest.fixture
def usage_targets(access_token):
    user = create_user(access_token, username=unique_name("usage_window"))
    admin = create_admin(access_token, username=unique_name("usage_window_admin"))
    yield {"user_id": user["id"], "admin_username": admin["username"]}
    delete_user(access_token, user["username"])
    delete_admin(access_token, admin["username"])


@pytest.mark.parametrize(
    "path",
    [
        "/api/user/by-id/{user_id}/usage",
        "/api/users/sub_update/chart",
        "/api/admin/{admin_username}/usage",
        "/api/node/usage",
        "/api/node/user_counts/online",
        "/api/node/999999/stats",
    ],
)
def test_twenty_six_years_of_hourly_buckets_is_refused(access_token, usage_targets, path):
    response = client.get(
        path.format(**usage_targets),
        headers=auth_headers(access_token),
        params=_window(TWENTY_SIX_YEARS, "hour"),
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "buckets" in response.json()["detail"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/user/by-id/{user_id}/usage",
        "/api/users/sub_update/chart",
        "/api/admin/{admin_username}/usage",
        "/api/node/usage",
        "/api/node/user_counts/online",
        "/api/node/999999/stats",
    ],
)
def test_the_dashboard_all_time_range_by_day_is_served(access_token, usage_targets, path):
    response = client.get(path.format(**usage_targets), headers=auth_headers(access_token), params=_all_time())
    assert response.status_code == status.HTTP_200_OK, response.text
