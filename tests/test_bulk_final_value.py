from datetime import UTC, datetime, timedelta

from app.fork.crud.bulk_result_bounds import (
    STORABLE_DATA_LIMIT_MAX,
    STORABLE_EXPIRE_MAX,
    STORABLE_EXPIRE_MIN,
    bulk_final_value,
)


def test_bulk_final_value_keeps_results_inside_the_storable_range():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert bulk_final_value("expire", start, 60) == start + timedelta(seconds=60)
    assert bulk_final_value("expire", STORABLE_EXPIRE_MAX - timedelta(seconds=5), 5) == STORABLE_EXPIRE_MAX
    assert bulk_final_value("expire", STORABLE_EXPIRE_MIN + timedelta(seconds=5), -5) == STORABLE_EXPIRE_MIN
    assert bulk_final_value("data_limit", STORABLE_DATA_LIMIT_MAX - 5, 5) == STORABLE_DATA_LIMIT_MAX


def test_bulk_final_value_rejects_results_outside_the_storable_range_and_python_overflow():
    assert bulk_final_value("expire", STORABLE_EXPIRE_MAX, 1) is None
    assert bulk_final_value("expire", STORABLE_EXPIRE_MIN, -1) is None
    assert bulk_final_value("expire", datetime(9800, 1, 1, tzinfo=UTC), 10**12) is None
    assert bulk_final_value("expire", datetime(2, 1, 1, tzinfo=UTC), -(10**12)) is None
    assert bulk_final_value("data_limit", STORABLE_DATA_LIMIT_MAX, 1) is None
