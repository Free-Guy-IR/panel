from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.jobs import record_usages


@pytest.mark.asyncio
async def test_failed_flushes_keep_a_bounded_number_of_pending_buckets(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(record_usages, "_pending_user_usage_history", defaultdict(int))
    monkeypatch.setattr(record_usages, "_user_usage_history_last_flush", 0.0)
    monkeypatch.setattr(record_usages, "get_dialect", AsyncMock(return_value="postgresql"))
    monkeypatch.setattr(record_usages, "_write_node_user_usage_params", AsyncMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(record_usages, "_monotonic_now", lambda: 1.0)
    monkeypatch.setattr(record_usages.usage_settings, "user_usage_history_flush_interval", 60)
    start = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)
    for index in range(30):
        bucket = start + timedelta(minutes=10 * index)
        rows = [{"uid": uid, "value": 5, "node_id": 1, "created_at": bucket, "epoch": 0} for uid in range(1, 101)]
        try:
            await record_usages.record_user_stats(rows)
        except RuntimeError:
            pass
    buckets = {key[0] for key in record_usages._pending_user_usage_history}
    assert len(buckets) <= 7, (len(buckets), len(record_usages._pending_user_usage_history))
