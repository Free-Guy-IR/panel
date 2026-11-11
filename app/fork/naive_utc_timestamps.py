from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import event
from sqlalchemy.pool import Pool

POSTGRES_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)
ONE_MICROSECOND = timedelta(microseconds=1)
POSITIVE_INFINITY = datetime.combine(date.max, time.max)
NEGATIVE_INFINITY = datetime.combine(date.min, time.min)
POSITIVE_INFINITY_MICROSECONDS = 2**63 - 1
NEGATIVE_INFINITY_MICROSECONDS = -(2**63)


def encode_timestamptz(value: datetime | date) -> tuple[int]:
    if not isinstance(value, date):
        raise TypeError(f"expected a datetime.date or datetime.datetime instance, got {type(value).__name__!r}")
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day, tzinfo=UTC)
    elif value == POSITIVE_INFINITY:
        return (POSITIVE_INFINITY_MICROSECONDS,)
    elif value == NEGATIVE_INFINITY:
        return (NEGATIVE_INFINITY_MICROSECONDS,)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return ((value - POSTGRES_EPOCH) // ONE_MICROSECOND,)


def decode_timestamptz(value: tuple[int]) -> datetime:
    (microseconds,) = value
    if microseconds == POSITIVE_INFINITY_MICROSECONDS:
        return POSITIVE_INFINITY
    if microseconds == NEGATIVE_INFINITY_MICROSECONDS:
        return NEGATIVE_INFINITY
    return POSTGRES_EPOCH + timedelta(microseconds=microseconds)


def _is_asyncpg_connection(dbapi_connection) -> bool:
    driver_connection = getattr(dbapi_connection, "driver_connection", None)
    return type(driver_connection).__module__.split(".", 1)[0] == "asyncpg"


def _install_codec(dbapi_connection, _connection_record) -> None:
    if not _is_asyncpg_connection(dbapi_connection):
        return
    dbapi_connection.run_async(
        lambda connection: connection.set_type_codec(
            "timestamptz",
            schema="pg_catalog",
            encoder=encode_timestamptz,
            decoder=decode_timestamptz,
            format="tuple",
        )
    )


def install_naive_utc_timestamps() -> None:
    if not event.contains(Pool, "connect", _install_codec):
        event.listen(Pool, "connect", _install_codec)
