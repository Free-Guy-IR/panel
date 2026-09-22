from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.db.crud.user import _cleanup_target_user_conditions

TEHRAN = timezone(timedelta(hours=3, minutes=30))
TEHRAN_MIDNIGHT = datetime(2026, 4, 2, 0, 0, 0, tzinfo=TEHRAN)
SAME_MOMENT_UTC = datetime(2026, 4, 1, 20, 30, 0)


def _bound_values(conditions):
    values = []
    for condition in conditions:
        for param in getattr(condition, "right", None) is not None and [condition.right] or []:
            value = getattr(param, "value", None)
            if isinstance(value, datetime):
                values.append(value)
    return values


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


@pytest.mark.parametrize("target", ["expired", "limited", "on_hold", "disabled"])
def test_an_offset_aware_bound_is_converted_to_utc_not_relabelled(target):
    conditions = _cleanup_target_user_conditions(expired_before=TEHRAN_MIDNIGHT, target=target)

    values = [_naive(value) for value in _bound_values(conditions)]

    assert values, f"no datetime bound was produced for target {target}"
    assert _naive(SAME_MOMENT_UTC) in values
    assert datetime(2026, 4, 2, 0, 0, 0) not in values


@pytest.mark.parametrize("target", ["expired", "limited", "on_hold", "disabled"])
def test_both_bounds_are_converted_for_every_target(target):
    conditions = _cleanup_target_user_conditions(
        expired_after=TEHRAN_MIDNIGHT, expired_before=TEHRAN_MIDNIGHT, target=target
    )

    values = [_naive(value) for value in _bound_values(conditions)]

    assert len(values) == 2
    assert all(value == _naive(SAME_MOMENT_UTC) for value in values)


@pytest.mark.parametrize("target", ["expired", "limited", "on_hold", "disabled"])
def test_a_utc_bound_survives_unchanged(target):
    moment = datetime(2026, 4, 1, 20, 30, 0, tzinfo=UTC)

    conditions = _cleanup_target_user_conditions(expired_before=moment, target=target)

    values = [_naive(value) for value in _bound_values(conditions)]

    assert values == [datetime(2026, 4, 1, 20, 30, 0)]
