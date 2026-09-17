import importlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.fork.jobs.traffic_log_purge import surviving_row_ids
from app.fork.models.traffic_log import TrafficLogRecord
from app.fork.traffic_log.collector import Bucket, TrafficCollector, bucket_start_of
from tests.api import GetTestDB


@pytest.fixture(autouse=True)
async def _empty_traffic_log():
    async with GetTestDB() as db:
        await db.execute(TrafficLogRecord.__table__.delete())
        await db.commit()
    yield
    async with GetTestDB() as db:
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

    async with GetTestDB() as db:
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

    collector.forget_buckets(None, keep_after=now, since_ingest=500)
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

    async with GetTestDB() as db:
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

    collector.forget_buckets(None, keep_after=now, since_ingest=500)
    collector.detach_missing_rows(surviving)
    carried = collector._buckets[_key("survived.example", start)]

    assert carried.row_id == row_id
    assert carried.flushed_hits == 6

    async with GetTestDB() as db:
        total = await db.scalar(select(func.count()).select_from(TrafficLogRecord))
    assert total == 1


@pytest.mark.asyncio
async def test_a_bucket_newer_than_the_cutoff_still_forgets_a_row_the_ceiling_deleted():
    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    cutoff = now - timedelta(hours=48)
    collector = TrafficCollector()

    async with GetTestDB() as db:
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

    async with GetTestDB() as db:
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

    async with GetTestDB() as db:
        total = await db.scalar(select(func.count()).select_from(TrafficLogRecord))
    assert total == 0


@pytest.mark.asyncio
async def test_the_job_itself_detaches_rows_it_deleted(monkeypatch: pytest.MonkeyPatch):
    from app.fork import traffic_log
    from app.fork.jobs import traffic_log_purge as purge_module
    from app.fork.jobs.traffic_log_purge import purge_traffic_log

    monkeypatch.setattr(purge_module, "GetDB", GetTestDB)

    now = datetime.now(UTC).replace(microsecond=0)
    old_start = bucket_start_of(now - timedelta(hours=72))
    collector = TrafficCollector()

    async def retention():
        return 48

    collector.effective_retention_hours = retention

    async with GetTestDB() as db:
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

    async with GetTestDB() as db:
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
        async with collector.suspend_flush(), GetTestDB() as db:
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


@pytest.mark.asyncio
async def test_the_scheduled_path_reseeds_the_watermark_when_it_empties_the_table():
    from app.fork.jobs.traffic_log_purge import purge_before, reconcile_buckets

    now = datetime.now(UTC).replace(microsecond=0)
    old_start = bucket_start_of(now - timedelta(hours=72))
    cutoff = now - timedelta(hours=48)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        row_id = await _insert(db, old_start, "sweep.example", 3)
        await db.commit()

    collector._max_row_id = row_id
    collector._ceiling_floor = row_id
    collector._seen_ids = {7, 8, 9}

    async with GetTestDB() as db:
        removed, _ = await purge_before(db, cutoff)
        assert removed == 1
        await reconcile_buckets(db, collector, cutoff)

    assert collector._max_row_id == 0
    assert collector._ceiling_floor == 0
    assert collector._seen_ids == set()


@pytest.mark.asyncio
async def test_deleting_the_highest_row_pulls_the_watermark_back_down():
    from app.fork.jobs.traffic_log_purge import reconcile_buckets

    now = datetime.now(UTC).replace(microsecond=0)
    old_start = bucket_start_of(now - timedelta(hours=72))
    recent_start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        low_id = await _insert(db, old_start, "kept.example", 1)
        high_id = await _insert(db, recent_start, "doomed.example", 1)
        await db.commit()
        assert high_id > low_id

        collector._max_row_id = high_id
        collector._ceiling_floor = high_id

        await db.execute(TrafficLogRecord.__table__.delete().where(TrafficLogRecord.id == high_id))
        await db.commit()

        await reconcile_buckets(db, collector, None, keep_after=now, since_ingest=0)

    assert collector._max_row_id == low_id
    assert collector._ceiling_floor == low_id


@pytest.mark.asyncio
async def test_a_straddling_bucket_whose_row_was_purged_is_redated_to_the_boundary():
    from app.fork.jobs.traffic_log_purge import purge_before, reconcile_buckets

    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now - timedelta(hours=49))
    cutoff = now - timedelta(hours=48)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        row_id = await _insert(db, start, "straddle.example", 3)
        await db.commit()

        collector._buckets = {
            _key("straddle.example", start): Bucket(
                first_seen=start,
                last_seen=now,
                hits=9,
                route="DIRECT",
                row_id=row_id,
                flushed_hits=3,
                last_ingest=42,
            )
        }

        removed, _ = await purge_before(db, cutoff)
        assert removed == 1
        await reconcile_buckets(db, collector, cutoff)

    bucket = collector._buckets[_key("straddle.example", start)]
    assert bucket.row_id is None
    assert bucket.flushed_hits == 0
    assert bucket.hits == 9
    assert bucket.first_seen == cutoff
    assert bucket.first_seen <= bucket.last_seen


@pytest.mark.asyncio
async def test_the_ceiling_evictor_detaches_the_rows_it_deleted(monkeypatch: pytest.MonkeyPatch):
    collector_module = importlib.import_module("app.fork.traffic_log.collector")

    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        evicted_id = await _insert(db, start, "evicted.example", 3)
        kept_id = await _insert(db, start, "kept.example", 2)
        await db.commit()

    collector._buckets = {
        _key("evicted.example", start): Bucket(
            first_seen=now, last_seen=now, hits=9, route="DIRECT", row_id=evicted_id, flushed_hits=3, last_ingest=5
        ),
        _key("kept.example", start): Bucket(
            first_seen=now, last_seen=now, hits=4, route="DIRECT", row_id=kept_id, flushed_hits=2, last_ingest=6
        ),
    }

    await collector._evict(evicted_id)

    evicted = collector._buckets[_key("evicted.example", start)]
    kept = collector._buckets[_key("kept.example", start)]

    assert evicted.row_id is None
    assert evicted.flushed_hits == 0
    assert evicted.hits == 9
    assert kept.row_id == kept_id
    assert kept.flushed_hits == 2

    async with GetTestDB() as db:
        remaining = (await db.scalars(select(TrafficLogRecord.id))).all()
    assert list(remaining) == [kept_id]


@pytest.mark.asyncio
async def test_the_ceiling_evictor_keeps_a_bucket_whose_row_it_could_not_delete(monkeypatch: pytest.MonkeyPatch):
    from app.fork.jobs import traffic_log_purge as purge_module

    collector_module = importlib.import_module("app.fork.traffic_log.collector")

    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    async def half_finished(db, threshold):
        return 1, True

    monkeypatch.setattr(purge_module, "enforce_ceiling", half_finished)

    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        row_id = await _insert(db, start, "survivor.example", 3)
        await db.commit()

    collector._buckets = {
        _key("survivor.example", start): Bucket(
            first_seen=now, last_seen=now, hits=9, route="DIRECT", row_id=row_id, flushed_hits=3, last_ingest=5
        )
    }

    await collector._evict(row_id)

    bucket = collector._buckets[_key("survivor.example", start)]
    assert bucket.row_id == row_id
    assert bucket.flushed_hits == 3


@pytest.mark.asyncio
async def test_an_incomplete_purge_keeps_a_bucket_whose_stored_row_survived():
    from app.fork.jobs.traffic_log_purge import reconcile_buckets

    now = datetime.now(UTC).replace(microsecond=0)
    old_start = bucket_start_of(now - timedelta(hours=72))
    cutoff = now - timedelta(hours=48)
    key = _key("leftover.example", old_start)

    def fresh_collector(row_id):
        collector = TrafficCollector()
        collector._buckets = {
            key: Bucket(
                first_seen=old_start,
                last_seen=old_start + timedelta(minutes=1),
                hits=7,
                route="DIRECT",
                row_id=row_id,
                flushed_hits=4,
                last_ingest=10,
            )
        }
        return collector

    async with GetTestDB() as db:
        row_id = await _insert(db, old_start, "leftover.example", 4)
        await db.commit()

        kept = fresh_collector(row_id)
        await reconcile_buckets(db, kept, cutoff, keep_stored_rows=True)

        dropped = fresh_collector(row_id)
        await reconcile_buckets(db, dropped, cutoff)

    assert key in kept._buckets
    assert kept._buckets[key].row_id == row_id
    assert kept._buckets[key].flushed_hits == 4
    assert key not in dropped._buckets


@pytest.mark.asyncio
async def test_the_owner_purge_keeps_a_straddling_bucket_the_watermark_would_have_dropped():
    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now - timedelta(minutes=2))
    cutoff = now - timedelta(minutes=1)
    collector = TrafficCollector()
    key = _key("straddling.example", start)
    collector._buckets = {
        key: Bucket(
            first_seen=start,
            last_seen=now,
            hits=9,
            route="DIRECT",
            row_id=None,
            flushed_hits=0,
            last_ingest=40,
        )
    }

    collector.forget_buckets(cutoff, since_ingest=100)

    bucket = collector._buckets[key]
    assert bucket.hits == 9
    assert bucket.first_seen == cutoff
    assert bucket.last_seen == now


@pytest.mark.asyncio
async def test_a_failed_eviction_is_repaired_by_the_next_flush(monkeypatch: pytest.MonkeyPatch):
    from app.fork.jobs import traffic_log_purge as purge_module

    collector_module = importlib.import_module("app.fork.traffic_log.collector")
    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    async def explode(db, wanted):
        raise RuntimeError("the reconciliation query failed after the delete committed")

    monkeypatch.setattr(purge_module, "surviving_row_ids", explode)

    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()
    collector._adopt_until = None

    async with GetTestDB() as db:
        row_id = await _insert(db, start, "repaired.example", 3)
        await db.commit()

    collector._buckets = {
        _key("repaired.example", start): Bucket(
            first_seen=now, last_seen=now, hits=9, route="DIRECT", row_id=row_id, flushed_hits=3, last_ingest=5
        )
    }

    await collector._evict(row_id)

    assert collector._rows_dirty is True
    assert collector._buckets[_key("repaired.example", start)].row_id == row_id

    monkeypatch.undo()
    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    await collector._flush_once()

    bucket = collector._buckets[_key("repaired.example", start)]
    assert collector._rows_dirty is False

    async with GetTestDB() as db:
        rows = (await db.scalars(select(TrafficLogRecord))).all()
    assert len(rows) == 1
    assert rows[0].hits == 9
    assert bucket.row_id == rows[0].id
    assert bucket.flushed_hits == 9


@pytest.mark.asyncio
async def test_a_row_written_before_this_process_started_is_adopted_not_duplicated(monkeypatch: pytest.MonkeyPatch):
    collector_module = importlib.import_module("app.fork.traffic_log.collector")
    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        row_id = await _insert(db, start, "restarted.example", 4)
        await db.commit()

    collector._buckets = {
        _key("restarted.example", start): Bucket(
            first_seen=now, last_seen=now, hits=3, route="DIRECT", row_id=None, flushed_hits=0, last_ingest=1
        )
    }

    await collector._flush_once()

    bucket = collector._buckets[_key("restarted.example", start)]
    assert bucket.row_id == row_id
    assert bucket.hits == 7

    async with GetTestDB() as db:
        rows = (await db.scalars(select(TrafficLogRecord))).all()
    assert len(rows) == 1
    assert rows[0].id == row_id
    assert rows[0].hits == 7


@pytest.mark.asyncio
async def test_a_quiet_restart_still_adopts_the_row_when_traffic_returns(monkeypatch: pytest.MonkeyPatch):
    collector_module = importlib.import_module("app.fork.traffic_log.collector")
    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        row_id = await _insert(db, start, "quiet.example", 6)
        await db.commit()

    await collector._flush_once()

    collector._buckets = {
        _key("quiet.example", start): Bucket(
            first_seen=now, last_seen=now, hits=2, route="DIRECT", row_id=None, flushed_hits=0, last_ingest=1
        )
    }

    await collector._flush_once()

    bucket = collector._buckets[_key("quiet.example", start)]
    assert bucket.row_id == row_id
    assert bucket.hits == 8

    async with GetTestDB() as db:
        rows = (await db.scalars(select(TrafficLogRecord))).all()
    assert len(rows) == 1
    assert rows[0].hits == 8


@pytest.mark.asyncio
async def test_an_adoption_that_fails_is_retried_instead_of_writing_a_twin(monkeypatch: pytest.MonkeyPatch):
    collector_module = importlib.import_module("app.fork.traffic_log.collector")
    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        row_id = await _insert(db, start, "retried.example", 5)
        await db.commit()

    collector._buckets = {
        _key("retried.example", start): Bucket(
            first_seen=now, last_seen=now, hits=4, route="DIRECT", row_id=None, flushed_hits=0, last_ingest=1
        )
    }

    original = TrafficCollector._adoptable
    calls = {"count": 0}

    def explode_once(self, boundary):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("the adoption query failed")
        return original(self, boundary)

    monkeypatch.setattr(TrafficCollector, "_adoptable", explode_once)

    with pytest.raises(RuntimeError):
        await collector._flush_once()

    assert collector._adopt_until is not None

    monkeypatch.setattr(TrafficCollector, "_adoptable", original)
    await collector._flush_once()

    bucket = collector._buckets[_key("retried.example", start)]
    assert bucket.row_id == row_id
    assert bucket.hits == 9

    async with GetTestDB() as db:
        rows = (await db.scalars(select(TrafficLogRecord))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_two_windows_never_adopt_the_same_stored_row(monkeypatch: pytest.MonkeyPatch):
    collector_module = importlib.import_module("app.fork.traffic_log.collector")
    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    now = datetime.now(UTC).replace(microsecond=0)
    current = bucket_start_of(now)
    previous = current - timedelta(minutes=5)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        row_id = await _insert(db, previous, "twowindows.example", 3)
        await db.commit()

    collector._buckets = {
        _key("twowindows.example", previous): Bucket(
            first_seen=previous, last_seen=previous, hits=1, route="DIRECT", row_id=None, flushed_hits=0, last_ingest=1
        ),
        _key("twowindows.example", current): Bucket(
            first_seen=now, last_seen=now, hits=2, route="DIRECT", row_id=None, flushed_hits=0, last_ingest=2
        ),
    }

    await collector._flush_once()

    newer = collector._buckets[_key("twowindows.example", current)]
    assert newer.row_id is not None and newer.row_id != row_id
    assert newer.hits == 2

    async with GetTestDB() as db:
        rows = (await db.scalars(select(TrafficLogRecord).order_by(TrafficLogRecord.bucket_start))).all()
    assert len(rows) == 2
    assert rows[0].id == row_id
    assert rows[0].hits == 4
    assert rows[1].id == newer.row_id
    assert rows[1].hits == 2


@pytest.mark.asyncio
async def test_duplicate_stored_rows_are_merged_into_the_oldest_one(monkeypatch: pytest.MonkeyPatch):
    collector_module = importlib.import_module("app.fork.traffic_log.collector")
    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        first_id = await _insert(db, start, "merged.example", 3)
        second_id = await _insert(db, start, "merged.example", 5)
        await db.commit()

    assert second_id > first_id

    collector._buckets = {
        _key("merged.example", start): Bucket(
            first_seen=now, last_seen=now, hits=2, route="DIRECT", row_id=None, flushed_hits=0, last_ingest=1
        )
    }

    await collector._flush_once()

    bucket = collector._buckets[_key("merged.example", start)]
    assert bucket.row_id == first_id
    assert bucket.hits == 10

    async with GetTestDB() as db:
        rows = (await db.scalars(select(TrafficLogRecord))).all()
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].hits == 10

    collector._adopt_until = bucket_start_of(now)
    collector._buckets[_key("merged.example", start)].row_id = None
    collector._buckets[_key("merged.example", start)].flushed_hits = 0
    collector._buckets[_key("merged.example", start)].hits = 0

    await collector._flush_once()

    async with GetTestDB() as db:
        rows = (await db.scalars(select(TrafficLogRecord))).all()
    assert len(rows) == 1
    assert rows[0].hits == 10


@pytest.mark.asyncio
async def test_a_merge_survives_an_adoption_that_fails_right_after_it(monkeypatch: pytest.MonkeyPatch):
    collector_module = importlib.import_module("app.fork.traffic_log.collector")
    monkeypatch.setattr(collector_module, "GetDB", GetTestDB)

    now = datetime.now(UTC).replace(microsecond=0)
    start = bucket_start_of(now)
    collector = TrafficCollector()

    async with GetTestDB() as db:
        first_id = await _insert(db, start, "faulty.example", 3)
        await _insert(db, start, "faulty.example", 5)
        await db.commit()

    collector._buckets = {
        _key("faulty.example", start): Bucket(
            first_seen=now, last_seen=now, hits=2, route="DIRECT", row_id=None, flushed_hits=0, last_ingest=1
        )
    }

    original = TrafficCollector._adoptable
    calls = {"count": 0}

    def explode_on_the_adoption_pass(self, boundary):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("the adoption pass failed after the merge was committed")
        return original(self, boundary)

    monkeypatch.setattr(TrafficCollector, "_adoptable", explode_on_the_adoption_pass)

    with pytest.raises(RuntimeError):
        await collector._flush_once()

    bucket = collector._buckets[_key("faulty.example", start)]
    assert bucket.row_id is None
    assert bucket.hits == 2
    assert bucket.flushed_hits == 0

    async with GetTestDB() as db:
        rows = (await db.scalars(select(TrafficLogRecord))).all()
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].hits == 8

    monkeypatch.setattr(TrafficCollector, "_adoptable", original)
    await collector._flush_once()

    assert collector._buckets[_key("faulty.example", start)].row_id == first_id

    async with GetTestDB() as db:
        rows = (await db.scalars(select(TrafficLogRecord))).all()
    assert len(rows) == 1
    assert rows[0].id == first_id
    assert rows[0].hits == 10
