from datetime import UTC, date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import Pool

from app.fork.naive_utc_timestamps import (
    NEGATIVE_INFINITY,
    NEGATIVE_INFINITY_MICROSECONDS,
    POSITIVE_INFINITY,
    POSITIVE_INFINITY_MICROSECONDS,
    _install_codec,
    decode_timestamptz,
    encode_timestamptz,
    install_naive_utc_timestamps,
)
from tests.api import DATABASE_URL, engine

TEHRAN = timezone(timedelta(hours=3, minutes=30))
NEW_YORK_SUMMER = timezone(timedelta(hours=-4))
LITERALS = [
    "2026-10-17 12:34:56.123456+00",
    "2000-01-01 00:00:00+00",
    "1999-12-31 23:59:59.999999+00",
    "1970-01-01 00:00:00+00",
    "0001-01-01 00:00:00+00",
    "9999-12-31 23:59:59.999999+00",
    "infinity",
    "-infinity",
]

needs_postgresql = pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="the codec is installed on asyncpg connections only"
)


def test_a_naive_value_encodes_as_the_same_wall_time_in_utc():
    naive = datetime(2026, 10, 17, 12, 34, 56, 123456, tzinfo=UTC).replace(tzinfo=None)

    assert encode_timestamptz(naive) == encode_timestamptz(naive.replace(tzinfo=UTC))


@pytest.mark.parametrize("zone", [TEHRAN, NEW_YORK_SUMMER, UTC])
def test_an_aware_value_encodes_its_instant(zone):
    instant = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)

    assert encode_timestamptz(instant.astimezone(zone)) == encode_timestamptz(instant)


def test_a_date_is_midnight_utc():
    assert encode_timestamptz(date(2026, 10, 17)) == encode_timestamptz(datetime(2026, 10, 17, tzinfo=UTC))


def test_the_infinity_sentinels_match_asyncpg():
    assert encode_timestamptz(POSITIVE_INFINITY) == (POSITIVE_INFINITY_MICROSECONDS,)
    assert encode_timestamptz(NEGATIVE_INFINITY) == (NEGATIVE_INFINITY_MICROSECONDS,)
    assert decode_timestamptz((POSITIVE_INFINITY_MICROSECONDS,)) is POSITIVE_INFINITY
    assert decode_timestamptz((NEGATIVE_INFINITY_MICROSECONDS,)) is NEGATIVE_INFINITY
    assert encode_timestamptz(POSITIVE_INFINITY.replace(tzinfo=UTC)) != (POSITIVE_INFINITY_MICROSECONDS,)


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 17, 12, 34, 56, 123456, tzinfo=UTC),
        datetime(1999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(1970, 1, 1, tzinfo=UTC),
        datetime(1, 1, 1, tzinfo=UTC),
    ],
)
def test_finite_values_round_trip_as_aware_utc(moment):
    decoded = decode_timestamptz(encode_timestamptz(moment))

    assert decoded == moment
    assert decoded.tzinfo is UTC


def test_other_types_are_refused():
    with pytest.raises(TypeError):
        encode_timestamptz("2026-10-17")


def test_installing_twice_registers_one_listener():
    install_naive_utc_timestamps()
    install_naive_utc_timestamps()

    assert event.contains(Pool, "connect", _install_codec)


def test_a_connection_from_another_driver_is_left_alone():
    class Untouchable:
        def run_async(self, _fn):
            raise AssertionError("the codec must only be installed on asyncpg connections")

    for module in ("aiosqlite.core", "asyncmy.connection"):
        driver = type("Connection", (), {"__module__": module})()
        connection = Untouchable()
        connection.driver_connection = driver
        _install_codec(connection, SimpleNamespace())


async def _builtin_decoded(literals: list[str]) -> list[datetime]:
    import asyncpg

    raw = await asyncpg.connect(DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1))
    try:
        return [row[0] for row in await raw.fetch("select x::timestamptz from unnest($1::text[]) x", literals)]
    finally:
        await raw.close()


@needs_postgresql
@pytest.mark.asyncio
async def test_values_read_through_the_panel_engine_equal_asyncpgs_own_decoding():
    async with engine.connect() as connection:
        through_panel = (
            (
                await connection.execute(
                    text("select x::timestamptz from unnest(cast(:values as text[])) x"), {"values": LITERALS}
                )
            )
            .scalars()
            .all()
        )

    assert list(through_panel) == await _builtin_decoded(LITERALS)
