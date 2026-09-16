from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.db import GetDB
from app.fork.jobs.traffic_log_purge import surviving_row_ids
from app.fork.models.traffic_log import TrafficLogRecord
from app.fork.traffic_log.collector import Bucket, TrafficCollector, bucket_start_of


@pytest.fixture(autouse=True)
async def _empty_traffic_log():
    async with GetDB() as db:
        await db.execute(TrafficLogRecord.__table__.delete())
        await db.commit()
    yield
    async with GetDB() as db:
        await db.execute(TrafficLogRecord.__table__.delete())
        await db.commit()


def _key(host, start):
    return (7, None, 9001, "in", host, 443, "tcp", "DIRECT", False, start)


async def _insert(db, start, host, hits):
    record = TrafficLogRecord(
        bucket_start=start,
        node_id=9001,
        inbound_tag="in",
        host=host,
        port=443,
        protocol="tcp",
        route="DIRECT",
        refused=False,
        user_id=7,
        user_label=None,
        first_seen=start,
        last_seen=start,
        hits=hits,
    )
    db.add(record)
    await db.flush()
    return record.id


@pytest.mark.asyncio
async def test_a_bucket_whose_row_the_purge_deleted_is_written_again_not_lost():
    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetDB() as db:
        row_id = await _insert(db, start, "deleted.example", 6)
        await db.commit()

        collector._buckets = {
            _key("deleted.example", start): Bucket(
                first_seen=now - timedelta(minutes=1),
                last_seen=now + timedelta(seconds=10),
                hits=9,
                route="DIRECT",
                row_id=row_id,
                flushed_hits=6,
                last_ingest=600,
            )
        }

        await db.execute(TrafficLogRecord.__table__.delete().where(TrafficLogRecord.id == row_id))
        await db.commit()

        tracked = collector.tracked_row_ids()
        found = await db.scalars(select(TrafficLogRecord.id).where(TrafficLogRecord.id.in_(tracked)))
        surviving = set(found.all())

    assert surviving == set()

    collector.forget_buckets(None, keep_after=now, since_ingest=500, storage_emptied=False)
    collector.detach_missing_rows(surviving)
    carried = collector._buckets[_key("deleted.example", start)]

    assert carried.row_id is None
    assert carried.flushed_hits == 0
    assert carried.hits == 9
    assert carried.first_seen <= carried.last_seen


@pytest.mark.asyncio
async def test_a_bucket_whose_row_survived_is_not_written_a_second_time():
    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetDB() as db:
        row_id = await _insert(db, start, "survived.example", 6)
        await db.commit()

        collector._buckets = {
            _key("survived.example", start): Bucket(
                first_seen=now - timedelta(minutes=1),
                last_seen=now + timedelta(seconds=10),
                hits=9,
                route="DIRECT",
                row_id=row_id,
                flushed_hits=6,
                last_ingest=600,
            )
        }

        tracked = collector.tracked_row_ids()
        found = await db.scalars(select(TrafficLogRecord.id).where(TrafficLogRecord.id.in_(tracked)))
        surviving = set(found.all())

    assert surviving == {row_id}

    collector.forget_buckets(None, keep_after=now, since_ingest=500, storage_emptied=False)
    collector.detach_missing_rows(surviving)
    carried = collector._buckets[_key("survived.example", start)]

    assert carried.row_id == row_id
    assert carried.flushed_hits == 6

    async with GetDB() as db:
        total = await db.scalar(select(func.count()).select_from(TrafficLogRecord))
    assert total == 1


@pytest.mark.asyncio
async def test_a_bucket_newer_than_the_cutoff_still_forgets_a_row_the_ceiling_deleted():
    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    cutoff = now - timedelta(hours=48)
    collector = TrafficCollector()

    async with GetDB() as db:
        row_id = await _insert(db, start, "ceiling.example", 5)
        await db.commit()
        collector._buckets = {
            _key("ceiling.example", start): Bucket(
                first_seen=now,
                last_seen=now,
                hits=8,
                route="DIRECT",
                row_id=row_id,
                flushed_hits=5,
                last_ingest=900,
            )
        }
        await db.execute(TrafficLogRecord.__table__.delete().where(TrafficLogRecord.id == row_id))
        await db.commit()
        surviving = await surviving_row_ids(db, collector.tracked_row_ids())

    assert surviving == set()

    collector.forget_buckets(cutoff)
    detached = collector.detach_missing_rows(surviving)
    bucket = collector._buckets[_key("ceiling.example", start)]

    assert detached == 1
    assert bucket.row_id is None
    assert bucket.flushed_hits == 0
    assert bucket.hits == 8


@pytest.mark.asyncio
async def test_the_scheduled_purge_detaches_rows_it_deleted():
    from app.fork.jobs.traffic_log_purge import purge_before, reconcile_buckets

    now = datetime.now(UTC).replace(microsecond=0)
    old_start = bucket_start_of(now - timedelta(hours=72))
    cutoff = now - timedelta(hours=48)
    collector = TrafficCollector()

    async with GetDB() as db:
        row_id = await _insert(db, old_start, "expired.example", 4)
        await db.commit()

        collector._buckets = {
            _key("expired.example", old_start): Bucket(
                first_seen=old_start,
                last_seen=now,
                hits=7,
                route="DIRECT",
                row_id=row_id,
                flushed_hits=4,
                last_ingest=10,
            )
        }

        expired, _ = await purge_before(db, cutoff)
        assert expired == 1

        detached = await reconcile_buckets(db, collector, cutoff)

    assert detached == 1
    bucket = collector._buckets[_key("expired.example", old_start)]
    assert bucket.row_id is None
    assert bucket.flushed_hits == 0
    assert bucket.hits == 7

    async with GetDB() as db:
        total = await db.scalar(select(func.count()).select_from(TrafficLogRecord))
    assert total == 0


@pytest.mark.asyncio
async def test_the_job_itself_detaches_rows_it_deleted(monkeypatch: pytest.MonkeyPatch):
    from app.fork import traffic_log
    from app.fork.jobs.traffic_log_purge import purge_traffic_log

    now = datetime.now(UTC).replace(microsecond=0)
    old_start = bucket_start_of(now - timedelta(hours=72))
    collector = TrafficCollector()

    async def retention():
        return 48

    collector.effective_retention_hours = retention

    async with GetDB() as db:
        row_id = await _insert(db, old_start, "job.example", 4)
        await db.commit()

    collector._buckets = {
        _key("job.example", old_start): Bucket(
            first_seen=old_start,
            last_seen=now,
            hits=7,
            route="DIRECT",
            row_id=row_id,
            flushed_hits=4,
            last_ingest=10,
        )
    }
    monkeypatch.setattr(traffic_log, "collector", collector)

    await purge_traffic_log()

    bucket = collector._buckets[_key("job.example", old_start)]
    assert bucket.row_id is None
    assert bucket.flushed_hits == 0
    assert bucket.hits == 7


@pytest.mark.asyncio
async def test_the_flush_lock_keeps_a_concurrent_flush_out_of_the_reconciliation():
    import asyncio

    from app.fork.jobs.traffic_log_purge import purge_before, reconcile_buckets

    now = datetime.now(UTC).replace(microsecond=0)
    old_start = bucket_start_of(now - timedelta(hours=72))
    cutoff = now - timedelta(hours=48)
    collector = TrafficCollector()
    order = []

    async with GetDB() as db:
        row_id = await _insert(db, old_start, "raced.example", 4)
        await db.commit()

    collector._buckets = {
        _key("raced.example", old_start): Bucket(
            first_seen=old_start,
            last_seen=now,
            hits=7,
            route="DIRECT",
            row_id=row_id,
            flushed_hits=4,
            last_ingest=10,
        )
    }

    async def reconcile():
        async with collector.suspend_flush(), GetDB() as db:
            order.append("reconcile-start")
            await purge_before(db, cutoff)
            await asyncio.sleep(0.05)
            await reconcile_buckets(db, collector, cutoff)
            order.append("reconcile-end")

    async def competing_flush():
        await asyncio.sleep(0.01)
        async with collector.suspend_flush():
            order.append("flush")
            for bucket in collector._buckets.values():
                bucket.row_id = 999999
                bucket.flushed_hits = bucket.hits

    await asyncio.gather(reconcile(), competing_flush())

    assert order == ["reconcile-start", "reconcile-end", "flush"]
    bucket = collector._buckets[_key("raced.example", old_start)]
    assert bucket.row_id == 999999
    assert bucket.hits == 7
