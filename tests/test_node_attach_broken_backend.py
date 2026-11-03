from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PasarGuardNodeBridge import Health, NodeAPIError
from PasarGuardNodeBridge.storage import LifecycleStatus

from app.operation.node import NodeOperation


def _pg_node(health: Health, observed: LifecycleStatus | None, backend_stats_error: Exception | None):
    state = None
    if observed is not None:
        state = SimpleNamespace(observed=observed, desired=LifecycleStatus.HEALTHY, epoch=3)
    node = SimpleNamespace(
        get_lifecycle_state=AsyncMock(return_value=state),
        info=AsyncMock(return_value=SimpleNamespace(started=True, node_version="0.5.5", core_version="25.10.1")),
        get_health=AsyncMock(return_value=health),
        get_backend_stats=AsyncMock(side_effect=backend_stats_error),
        connect=AsyncMock(),
        update_observed_lifecycle=AsyncMock(),
    )
    return node


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("health", "observed"),
    [
        (Health.BROKEN, None),
        (Health.HEALTHY, LifecycleStatus.BROKEN),
    ],
)
async def test_broken_backend_that_still_fails_is_not_reattached(health, observed):
    node = _pg_node(health, observed, NodeAPIError(500, "wg0: no such device"))

    result = await NodeOperation._attach_if_running(node, "n1")

    assert result is None
    node.get_backend_stats.assert_awaited_once()
    node.connect.assert_not_awaited()
    node.update_observed_lifecycle.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovered_broken_backend_is_reattached():
    node = _pg_node(Health.BROKEN, None, None)

    result = await NodeOperation._attach_if_running(node, "n1")

    assert result is not None
    node.get_backend_stats.assert_awaited_once()
    node.connect.assert_awaited_once_with("0.5.5", "25.10.1")


@pytest.mark.asyncio
async def test_healthy_backend_is_reattached_without_a_stats_probe():
    node = _pg_node(Health.HEALTHY, LifecycleStatus.HEALTHY, None)

    result = await NodeOperation._attach_if_running(node, "n1")

    assert result is not None
    node.get_backend_stats.assert_not_awaited()
    node.connect.assert_awaited_once()
