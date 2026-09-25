from datetime import UTC, datetime, timedelta

from app.models.stats import Period

MAX_USAGE_SPAN = {
    Period.minute: timedelta(days=1),
    Period.hour: timedelta(days=31),
    Period.day: timedelta(days=400),
    Period.month: timedelta(days=366 * 5),
}
CLOCK_SKEW = timedelta(minutes=5)


def ensure_usage_span(period: Period, start: datetime | None, end: datetime | None) -> None:
    if start is None:
        return
    limit = MAX_USAGE_SPAN[Period(period)]
    if (end or datetime.now(UTC)) - start > limit + CLOCK_SKEW:
        raise ValueError(
            f"a {Period(period).value} usage range may span at most {limit.days} day(s); "
            "use a coarser period or a shorter range"
        )
