from __future__ import annotations

import logging

import pytest

from app.jobs import record_usages


class _MutableNode:
    def __init__(self, usage_coefficient: float | None = 1.0, fail: bool = False):
        self.usage_coefficient = usage_coefficient
        self.fail = fail

    async def get_extra(self) -> dict:
        if self.fail:
            raise RuntimeError("extra unavailable")
        if self.usage_coefficient is None:
            return {"id": 1}
        return {"id": 1, "usage_coefficient": self.usage_coefficient}


@pytest.fixture(autouse=True)
def _clear_cache():
    record_usages._usage_coefficient_cache.clear()
    yield
    record_usages._usage_coefficient_cache.clear()


@pytest.mark.asyncio
async def test_coefficient_change_is_billed_on_the_very_next_cycle():
    node = _MutableNode(usage_coefficient=1.0)

    assert await record_usages._node_usage_coefficient(node, 20) == 1.0

    node.usage_coefficient = 2.5

    assert await record_usages._node_usage_coefficient(node, 20) == 2.5


@pytest.mark.asyncio
async def test_zero_coefficient_is_not_coerced_to_one():
    node = _MutableNode(usage_coefficient=0)

    assert await record_usages._node_usage_coefficient(node, 4242) == 0.0


@pytest.mark.asyncio
async def test_unreadable_coefficient_reuses_the_last_known_value_and_says_so(caplog):
    node = _MutableNode(usage_coefficient=2.0)

    assert await record_usages._node_usage_coefficient(node, 33) == 2.0

    node.fail = True
    caplog.set_level(logging.WARNING, logger="record-usages")

    assert await record_usages._node_usage_coefficient(node, 33) == 2.0
    assert "reusing last known value 2.0" in caplog.text


@pytest.mark.asyncio
async def test_unreadable_coefficient_with_no_history_falls_back_to_one_loudly(caplog):
    node = _MutableNode(fail=True)
    caplog.set_level(logging.ERROR, logger="record-usages")

    assert await record_usages._node_usage_coefficient(node, 41) == 1.0

    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "No usage coefficient available for node 41" in caplog.text
    assert 41 not in record_usages._usage_coefficient_cache


@pytest.mark.asyncio
async def test_missing_coefficient_key_is_not_a_silent_one(caplog):
    node = _MutableNode(usage_coefficient=None)
    caplog.set_level(logging.WARNING, logger="record-usages")

    assert await record_usages._node_usage_coefficient(node, 66) == 1.0
    assert "Node 66 reported no usage coefficient and none was ever seen" in caplog.text


@pytest.mark.asyncio
async def test_missing_coefficient_key_reuses_the_last_known_value(caplog):
    node = _MutableNode(usage_coefficient=2.0)

    assert await record_usages._node_usage_coefficient(node, 70) == 2.0

    node.usage_coefficient = None
    caplog.set_level(logging.ERROR, logger="record-usages")

    assert await record_usages._node_usage_coefficient(node, 70) == 2.0
    assert "reusing last known value 2.0" in caplog.text


@pytest.mark.asyncio
async def test_apply_usage_value_still_rounds_the_product():
    from app.fork.jobs.usage_coeff import apply_usage_value

    assert apply_usage_value(100, 2.5) == 250
    assert apply_usage_value(3, 0.5) == 2
    assert apply_usage_value(101, 1.0) == 101


@pytest.mark.asyncio
async def test_unusable_coefficient_reuses_last_known_instead_of_billing_at_one():
    node = _MutableNode(usage_coefficient=2.5)
    assert await record_usages._node_usage_coefficient(node, 1) == 2.5

    node.usage_coefficient = "2,5"
    assert await record_usages._node_usage_coefficient(node, 1) == 2.5


@pytest.mark.asyncio
async def test_unusable_coefficient_with_no_history_falls_back_to_one():
    node = _MutableNode(usage_coefficient="2,5")
    assert await record_usages._node_usage_coefficient(node, 77) == 1.0
