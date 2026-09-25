from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit

import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import create_user, delete_user


@pytest.fixture
def usage_path(access_token):
    user = create_user(access_token)
    yield urlsplit(user["subscription_url"]).path + "/usage"
    delete_user(access_token, user["username"])


def _get(path, period, days, end_days_ago=None):
    now = datetime.now(UTC)
    params = f"?period={period}&start={quote((now - timedelta(days=days)).isoformat())}"
    if end_days_ago is not None:
        params += f"&end={quote((now - timedelta(days=end_days_ago)).isoformat())}"
    return client.get(path + params)


@pytest.mark.parametrize(
    ("period", "days"),
    [("minute", 1 / 24), ("hour", 1), ("day", 7), ("day", 30), ("day", 90), ("hour", 31), ("month", 365 * 5)],
)
def test_the_ranges_the_subscription_page_and_clients_ask_for_are_served(usage_path, period, days):
    response = _get(usage_path, period, days)
    assert response.status_code == status.HTTP_200_OK, response.text


@pytest.mark.parametrize(
    ("period", "days"),
    [("minute", 2), ("hour", 32), ("day", 36_000), ("day", 401), ("month", 365 * 6)],
)
def test_a_range_longer_than_its_period_allows_is_refused(usage_path, period, days):
    response = _get(usage_path, period, days)
    assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text


def test_the_span_counts_from_start_to_the_given_end(usage_path):
    assert _get(usage_path, "hour", 60, end_days_ago=30).status_code == status.HTTP_200_OK
    assert _get(usage_path, "hour", 90, end_days_ago=30).status_code == status.HTTP_400_BAD_REQUEST
