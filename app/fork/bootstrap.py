from importlib import import_module, util

from app.utils.logger import get_logger

logger = get_logger("fork-bootstrap")

FORK_MODULES = ("cores", "subscription", "hooks")
REQUIRED_SUBSCRIPTION_FORMATS = ("openvpn", "l2tp")

_loaded = False


def _module_is_installed(qual: str) -> bool:
    try:
        return util.find_spec(qual) is not None
    except ImportError, ValueError:
        return False


def load_fork() -> None:
    global _loaded
    if _loaded:
        return
    for name in FORK_MODULES:
        qual = f"app.fork.{name}"
        if not _module_is_installed(qual):
            logger.warning(f"fork module {qual} is not present; skipping")
            continue
        try:
            import_module(qual)
        except Exception:
            logger.critical(
                f"fork module {qual} is present but failed to import; refusing to run with reduced capability"
            )
            raise
    _loaded = True
    verify_fork_capabilities()


def verify_fork_capabilities() -> None:
    from app.fork.registry import extra_subscription_formats

    formats = extra_subscription_formats()
    missing = [key for key in REQUIRED_SUBSCRIPTION_FORMATS if key not in formats]
    if missing:
        raise RuntimeError(f"fork subscription formats missing after load_fork(): {missing}")
