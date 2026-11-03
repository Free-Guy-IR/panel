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


def test_bulk_final_value_storable_edges_are_absolute():
    assert STORABLE_EXPIRE_MAX == datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    assert STORABLE_EXPIRE_MIN == datetime(1000, 1, 1, tzinfo=UTC)
    assert STORABLE_DATA_LIMIT_MAX == 9223372036854775807
    last_second = datetime(9999, 12, 31, 23, 59, 58, 500000, tzinfo=UTC)
    assert bulk_final_value("expire", last_second, 1) == datetime(9999, 12, 31, 23, 59, 59, 500000, tzinfo=UTC)
    assert bulk_final_value("expire", datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC), 1) is None
    first_second = datetime(1000, 1, 1, 0, 0, 1, 999999, tzinfo=UTC)
    assert bulk_final_value("expire", first_second, -1) == datetime(1000, 1, 1, 0, 0, 0, 999999, tzinfo=UTC)
    assert bulk_final_value("expire", first_second, -2) is None
    assert bulk_final_value("data_limit", 9223372036854775806, 1) == 9223372036854775807
    assert bulk_final_value("data_limit", 9223372036854775806, 2) is None
