from unittest.mock import AsyncMock

import pytest

from app.models.admin import AdminDetails
from app.operation.system import SystemOperation

COUNTS = {"total": 0, "active": 0, "disabled": 0, "on_hold": 0, "expired": 0, "limited": 0}


def _admin() -> AdminDetails:
    return AdminDetails(id=1, username="owner", is_sudo=True, is_owner=True)


@pytest.fixture(autouse=True)
def _unscoped_admin(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.operation.system.get_users_count_metrics", AsyncMock(return_value=(COUNTS, 0)))
    monkeypatch.setattr("app.operation.system.is_scope_all", lambda *a, **k: True)


@pytest.mark.asyncio
async def test_no_system_row_and_no_admin_scope_yields_nulls(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("app.operation.system.get_system_usage", AsyncMock(return_value=None))

    stats = await SystemOperation.get_system_users_stats(AsyncMock(), _admin())

    assert stats.incoming_bandwidth == 0
    assert stats.outgoing_bandwidth == 0
    assert stats.admin_used_traffic is None


@pytest.mark.asyncio
async def test_system_row_present_yields_raw_directional_counters(monkeypatch: pytest.MonkeyPatch):
    system = AsyncMock()
    system.uplink = 111
    system.downlink = 222
    monkeypatch.setattr("app.operation.system.get_system_usage", AsyncMock(return_value=system))

    stats = await SystemOperation.get_system_users_stats(AsyncMock(), _admin())

    assert stats.incoming_bandwidth == 111
    assert stats.outgoing_bandwidth == 222
    assert stats.admin_used_traffic is None
