import pytest
from PasarGuardNodeBridge import controller as bridge_controller, storage as bridge_storage

from app.fork import node_bridge_guard


def test_the_installed_bridge_carries_the_reliable_user_sync():
    node_bridge_guard.require_reliable_bridge()


@pytest.mark.parametrize(
    "owner, name",
    [
        (bridge_controller.Controller, "_deliver_claimed_users"),
        (bridge_controller.Controller, "full_sync_fence"),
        (bridge_controller.Controller, "wake_sync_worker"),
        (bridge_storage.InMemoryUserSyncStore, "begin_full_sync"),
        (bridge_storage.InMemoryUserSyncStore, "capture_queued"),
        (bridge_storage.InMemoryUserSyncStore, "retire_captured"),
        (bridge_storage.InMemoryUserSyncStore, "fence_state"),
        (bridge_storage.InMemoryUserSyncStore, "has_pending"),
    ],
)
def test_a_bridge_without_the_reliable_user_sync_refuses_to_start(monkeypatch, owner, name):
    monkeypatch.delattr(owner, name)
    with pytest.raises(RuntimeError, match=name):
        node_bridge_guard.require_reliable_bridge()


@pytest.mark.parametrize("version", ["0.8.1", "0.9.1+singbox", "garbage", ""])
def test_a_bridge_reporting_an_older_version_refuses_to_start(monkeypatch, version):
    monkeypatch.setattr("PasarGuardNodeBridge.__version__", version)
    with pytest.raises(RuntimeError, match="0.9.2"):
        node_bridge_guard.require_reliable_bridge()


def test_the_node_manager_checks_the_bridge_when_it_is_imported():
    import inspect

    import app.node

    assert "require_reliable_bridge()" in inspect.getsource(app.node)
