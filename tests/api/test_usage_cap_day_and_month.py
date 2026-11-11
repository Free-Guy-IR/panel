import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers

OVER_THE_CAP = {"start": "1880-01-01T00:00:00+00:00", "end": "2026-01-01T00:00:00+00:00"}


@pytest.mark.parametrize("path", ["/api/users/usage", "/api/users/counts/online", "/api/node/usage"])
def test_day_buckets_past_the_fork_cap_are_refused(access_token, path):
    response = client.get(path, headers=auth_headers(access_token), params={**OVER_THE_CAP, "period": "day"})
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text
    assert "at most 50000 buckets" in response.json()["detail"]


def test_month_buckets_over_the_same_range_stay_allowed(access_token):
    response = client.get(
        "/api/users/usage", headers=auth_headers(access_token), params={**OVER_THE_CAP, "period": "month"}
    )
    assert response.status_code == status.HTTP_200_OK, response.text
