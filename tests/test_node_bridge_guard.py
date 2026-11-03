import importlib

import pytest
from PasarGuardNodeBridge import (
    controller as bridge_controller,
    grpclib as bridge_grpclib,
    log_stream as bridge_log_stream,
    storage as bridge_storage,
)

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
        (bridge_grpclib.Node, "_probe_capabilities"),
        (bridge_grpclib, "RetirableChannel"),
        (bridge_log_stream, "deliver_stream_error"),
        (bridge_controller, "MAINTENANCE_TIMEOUTS"),
    ],
)
def test_a_bridge_without_the_reliable_user_sync_refuses_to_start(monkeypatch, owner, name):
    monkeypatch.delattr(owner, name)
    with pytest.raises(RuntimeError, match=name):
        node_bridge_guard.require_reliable_bridge()


def test_the_guard_requires_the_maintenance_timeouts_the_panel_relies_on():
    assert (bridge_controller, "MAINTENANCE_TIMEOUTS") in node_bridge_guard.REQUIRED_BRIDGE_API


@pytest.mark.parametrize(
    "version", ["0.8.1", "0.9.1+singbox", "0.9.2", "0.9.2+singbox", "0.9.2+singbox.2", "garbage", ""]
)
def test_a_bridge_reporting_an_older_version_refuses_to_start(monkeypatch, version):
    monkeypatch.setattr("PasarGuardNodeBridge.__version__", version)
    with pytest.raises(RuntimeError, match=r"needs 0\.9\.2\+singbox\.3 or newer"):
        node_bridge_guard.require_reliable_bridge()


@pytest.mark.parametrize(
    "owner, name",
    [
        (bridge_storage, "HeldRetryUserSyncStoreProtocol"),
        (bridge_storage.HeldRetryUserSyncStoreProtocol, "retire_held"),
        (bridge_storage.InMemoryUserSyncStore, "hold_users"),
        (bridge_storage.InMemoryUserSyncStore, "retire_held"),
    ],
)
def test_a_bridge_without_the_held_retry_api_refuses_to_start(monkeypatch, owner, name):
    monkeypatch.delattr(owner, name, raising=False)
    with pytest.raises(RuntimeError, match=name):
        node_bridge_guard.require_reliable_bridge()


def test_the_guard_requires_the_held_retry_api():
    required = {(getattr(owner, "__name__", owner), name) for owner, name in node_bridge_guard.REQUIRED_BRIDGE_API}
    assert ("PasarGuardNodeBridge.storage", "HeldRetryUserSyncStoreProtocol") in required
    assert "retire_held" in node_bridge_guard.REQUIRED_HELD_RETRY_PROTOCOL_API
    assert ("InMemoryUserSyncStore", "hold_users") in required
    assert ("InMemoryUserSyncStore", "retire_held") in required


@pytest.mark.parametrize("version", ["0.9.2+singbox.3", "0.9.2+singbox.4", "0.9.4+singbox", "0.9.4+singbox.1"])
def test_a_bridge_reporting_the_pinned_build_or_newer_starts(monkeypatch, version):
    monkeypatch.setattr("PasarGuardNodeBridge.__version__", version)
    node_bridge_guard.require_reliable_bridge()


def test_a_refusal_for_a_missing_api_names_the_first_bridge_build_this_panel_accepts(monkeypatch):
    monkeypatch.delattr(bridge_controller, "MAINTENANCE_TIMEOUTS")
    with pytest.raises(RuntimeError) as refused:
        node_bridge_guard.require_reliable_bridge()
    assert "install bridge 0.9.2+singbox.3 or newer" in str(refused.value)
    assert "0.9.2+singbox.2 or newer" not in str(refused.value)


def test_the_node_manager_checks_the_bridge_when_it_is_imported():
    import inspect

    import app.node

    assert "require_reliable_bridge()" in inspect.getsource(app.node)


def test_a_bridge_without_the_held_retry_protocol_refuses_at_the_guard_not_at_import(monkeypatch):
    try:
        with monkeypatch.context() as patched:
            patched.delattr(bridge_storage, "HeldRetryUserSyncStoreProtocol")
            reloaded = importlib.reload(node_bridge_guard)
            with pytest.raises(RuntimeError) as refused:
                reloaded.require_reliable_bridge()
        assert "HeldRetryUserSyncStoreProtocol" in str(refused.value)
        assert "install bridge 0.9.2+singbox.3 or newer" in str(refused.value)
    finally:
        importlib.reload(node_bridge_guard)
