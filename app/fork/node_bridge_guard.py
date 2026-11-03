import PasarGuardNodeBridge
from packaging.version import InvalidVersion, Version
from PasarGuardNodeBridge import controller, grpclib, log_stream, storage

MINIMUM_BRIDGE_VERSION = Version("0.9.2+singbox.3")
REQUIRED_BRIDGE_API = (
    (controller.Controller, "_deliver_claimed_users"),
    (controller.Controller, "full_sync_fence"),
    (controller.Controller, "wake_sync_worker"),
    (storage.InMemoryUserSyncStore, "begin_full_sync"),
    (storage.InMemoryUserSyncStore, "capture_queued"),
    (storage.InMemoryUserSyncStore, "retire_captured"),
    (storage.InMemoryUserSyncStore, "fence_state"),
    (storage.InMemoryUserSyncStore, "has_pending"),
    (storage, "HeldRetryUserSyncStoreProtocol"),
    (storage.InMemoryUserSyncStore, "hold_users"),
    (storage.InMemoryUserSyncStore, "retire_held"),
    (grpclib.Node, "_probe_capabilities"),
    (grpclib, "RetirableChannel"),
    (log_stream, "deliver_stream_error"),
    (controller, "MAINTENANCE_TIMEOUTS"),
)
REQUIRED_HELD_RETRY_PROTOCOL_API = ("retire_held",)


def _missing_bridge_api() -> list[str]:
    missing = [f"{owner.__name__}.{name}" for owner, name in REQUIRED_BRIDGE_API if not hasattr(owner, name)]
    protocol = getattr(storage, "HeldRetryUserSyncStoreProtocol", None)
    if protocol is not None:
        missing.extend(
            f"HeldRetryUserSyncStoreProtocol.{name}"
            for name in REQUIRED_HELD_RETRY_PROTOCOL_API
            if not hasattr(protocol, name)
        )
    return missing


def require_reliable_bridge() -> None:
    missing = _missing_bridge_api()
    if missing:
        raise RuntimeError(
            "The installed PasarGuardNodeBridge lacks the reliable user sync this panel needs "
            f"(missing {', '.join(missing)}); install bridge {MINIMUM_BRIDGE_VERSION} or newer"
        )
    reported = getattr(PasarGuardNodeBridge, "__version__", "")
    try:
        version = Version(reported)
    except InvalidVersion:
        version = None
    if version is None or version < MINIMUM_BRIDGE_VERSION:
        raise RuntimeError(
            f"The installed PasarGuardNodeBridge reports version {reported!r}; this panel needs "
            f"{MINIMUM_BRIDGE_VERSION} or newer"
        )
