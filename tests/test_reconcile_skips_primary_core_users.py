from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.fork.operation.node_extras import NodeExtraCoresMixin


class _Reconciler(NodeExtraCoresMixin):
    pass


@pytest.fixture
def requested_core_ids(monkeypatch):
    seen: list[set[int]] = []

    async def _map(db, core_ids):
        seen.append(set(core_ids))
        return {core_id: SimpleNamespace(type="xray") for core_id in core_ids}, {
            core_id: [f"user-of-{core_id}"] for core_id in core_ids
        }

    monkeypatch.setattr(_Reconciler, "_get_core_users_map", staticmethod(_map), raising=False)
    monkeypatch.setattr(
        _Reconciler,
        "_resolve_core_without_users",
        AsyncMock(return_value=SimpleNamespace(type="xray")),
    )
    monkeypatch.setattr(_Reconciler, "_remove_surplus_backends", AsyncMock(return_value=""))
    monkeypatch.setattr(_Reconciler, "_add_extra_cores", AsyncMock(return_value=""))
    return seen


def _node(primary: int, core_ids: list[int]):
    return SimpleNamespace(core_config_id=primary, name="n", id=1, _core_ids=core_ids)


@pytest.mark.asyncio
async def test_a_single_core_node_fetches_no_user_lists_at_all(requested_core_ids, monkeypatch):
    monkeypatch.setattr(_Reconciler, "_node_core_ids", staticmethod(lambda db_node: [9]))

    await _Reconciler._reconcile_extra_cores(AsyncMock(), AsyncMock(), _node(9, [9]))

    assert requested_core_ids == [set()]


@pytest.mark.asyncio
async def test_the_primary_core_is_never_in_the_user_fetch(requested_core_ids, monkeypatch):
    monkeypatch.setattr(_Reconciler, "_node_core_ids", staticmethod(lambda db_node: [9, 3]))

    await _Reconciler._reconcile_extra_cores(AsyncMock(), AsyncMock(), _node(9, [9, 3]))

    assert requested_core_ids == [{3}]
    assert 9 not in requested_core_ids[0]


@pytest.mark.asyncio
async def test_the_primary_core_type_still_reaches_the_surplus_check(requested_core_ids, monkeypatch):
    monkeypatch.setattr(_Reconciler, "_node_core_ids", staticmethod(lambda db_node: [9, 3]))

    await _Reconciler._reconcile_extra_cores(AsyncMock(), AsyncMock(), _node(9, [9, 3]))

    _Reconciler._resolve_core_without_users.assert_awaited_once_with(9)
    assert _Reconciler._remove_surplus_backends.await_args.args[-1] == "xray"


@pytest.mark.asyncio
async def test_extra_cores_still_receive_their_own_user_lists(requested_core_ids, monkeypatch):
    monkeypatch.setattr(_Reconciler, "_node_core_ids", staticmethod(lambda db_node: [9, 3, 6]))

    await _Reconciler._reconcile_extra_cores(AsyncMock(), AsyncMock(), _node(9, [9, 3, 6]))

    passed = _Reconciler._add_extra_cores.await_args.args[-1]
    assert {core_id for core_id, _, _ in passed} == {3, 6}
    assert all(users == [f"user-of-{core_id}"] for core_id, _, users in passed)
