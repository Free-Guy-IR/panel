import PasarGuardNodeBridge
from packaging.version import InvalidVersion, Version
from PasarGuardNodeBridge import controller, storage

MINIMUM_BRIDGE_VERSION = Version("0.9.2")
REQUIRED_BRIDGE_API = (
    (controller.Controller, "_deliver_claimed_users"),
    (controller.Controller, "full_sync_fence"),
    (controller.Controller, "wake_sync_worker"),
    (storage.InMemoryUserSyncStore, "begin_full_sync"),
    (storage.InMemoryUserSyncStore, "capture_queued"),
    (storage.InMemoryUserSyncStore, "retire_captured"),
    (storage.InMemoryUserSyncStore, "fence_state"),
    (storage.InMemoryUserSyncStore, "has_pending"),
)


def require_reliable_bridge() -> None:
    missing = [f"{owner.__name__}.{name}" for owner, name in REQUIRED_BRIDGE_API if not hasattr(owner, name)]
    if missing:
        raise RuntimeError(
            "The installed PasarGuardNodeBridge lacks the reliable user sync this panel needs "
            f"(missing {', '.join(missing)}); install bridge {MINIMUM_BRIDGE_VERSION}+singbox or newer"
        )
    reported = getattr(PasarGuardNodeBridge, "__version__", "")
    try:
        version = Version(reported)
    except InvalidVersion:
        version = None
    if version is None or Version(version.public) < MINIMUM_BRIDGE_VERSION:
        raise RuntimeError(
            f"The installed PasarGuardNodeBridge reports version {reported!r}; this panel needs "
            f"{MINIMUM_BRIDGE_VERSION}+singbox or newer"
        )
