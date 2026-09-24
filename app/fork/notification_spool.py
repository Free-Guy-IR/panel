import json
import os
import tempfile
import time
from collections import Counter
from pathlib import Path

from app.nats import is_multi_worker
from app.notification.nats_queue import NotificationQueue
from app.notification.queue_manager import get_queue, get_webhook_queue
from app.utils.logger import get_logger

logger = get_logger("notification-spool")

SPOOL_PATH = Path("/var/lib/pasarguard/notification_spool.json")
SPOOL_VERSION = 1
SPOOL_MAX_ITEMS = 10_000
RESTORING_MARKER = ".restoring-"
CORRUPT_MARKER = ".corrupt-"


class SpoolFileError(ValueError):
    pass


def _queues() -> dict[str, NotificationQueue]:
    return {"notifications": get_queue(), "webhooks": get_webhook_queue()}


def _summary(entries: list) -> str:
    counts = Counter(str(entry.get("queue")) if isinstance(entry, dict) else "?" for entry in entries)
    return ", ".join(f"{name}={count}" for name, count in sorted(counts.items())) or "none"


def _sibling(marker: str) -> Path:
    return SPOOL_PATH.with_name(f"{SPOOL_PATH.name}{marker}{os.getpid()}-{time.time_ns()}")


def _read_spool(path: Path) -> list:
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpoolFileError(f"not valid JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("version") != SPOOL_VERSION:
        raise SpoolFileError("unknown layout or version")
    entries = document.get("notifications")
    if not isinstance(entries, list):
        raise SpoolFileError("the notifications list is missing")
    return entries


def _write_atomically(path: Path, entries: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": SPOOL_VERSION, "notifications": entries}).encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _quarantine(path: Path, reason: str) -> None:
    target = _sibling(CORRUPT_MARKER)
    size = path.stat().st_size
    os.replace(path, target)
    logger.error(
        "Notification spool %s (%s bytes) is unusable (%s); it was moved to %s and none of it was sent",
        path,
        size,
        reason,
        target,
    )


def _valid_entries(entries: list, source: Path) -> list[dict]:
    queue_names = set(_queues())
    valid = []
    invalid = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("queue") in queue_names and isinstance(entry.get("item"), dict):
            valid.append(entry)
        else:
            invalid.append(entry)
    if invalid:
        target = _sibling(CORRUPT_MARKER)
        _write_atomically(target, invalid)
        logger.error(
            "Notification spool %s held %s unreadable entries; they were kept in %s and not sent",
            source,
            len(invalid),
            target,
        )
    return valid


def _existing_entries() -> list[dict]:
    if not SPOOL_PATH.exists():
        return []
    try:
        return _valid_entries(_read_spool(SPOOL_PATH), SPOOL_PATH)
    except SpoolFileError as exc:
        _quarantine(SPOOL_PATH, str(exc))
        return []


async def persist_notification_backlog() -> None:
    from app.notification.client import stop_notification_dispatcher

    await stop_notification_dispatcher()
    entries = [{"queue": name, "item": item} for name, queue in _queues().items() for item in queue.drain_nowait()]
    if not entries:
        return

    try:
        combined = _existing_entries() + entries
        if len(combined) > SPOOL_MAX_ITEMS:
            overflow = combined[SPOOL_MAX_ITEMS:]
            combined = combined[:SPOOL_MAX_ITEMS]
            logger.error(
                "Notification spool is capped at %s entries; %s newest unsent notification(s) (%s) are lost",
                SPOOL_MAX_ITEMS,
                len(overflow),
                _summary(overflow),
            )
        _write_atomically(SPOOL_PATH, combined)
    except OSError as exc:
        logger.error(
            "Could not save %s unsent notification(s) (%s) to %s: %s; they are lost with this process",
            len(entries),
            _summary(entries),
            SPOOL_PATH,
            exc,
        )
        return
    logger.warning(
        "Saved %s unsent notification(s) (%s) to %s; they are sent after the next start",
        len(entries),
        _summary(entries),
        SPOOL_PATH,
    )


def _claim(path: Path) -> Path | None:
    claimed = _sibling(RESTORING_MARKER)
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return None
    return claimed


def _unfinished_restores() -> list[Path]:
    if not SPOOL_PATH.parent.exists():
        return []
    return sorted(SPOOL_PATH.parent.glob(f"{SPOOL_PATH.name}{RESTORING_MARKER}*"))


async def _restore_file(claimed: Path) -> None:
    try:
        entries = _valid_entries(_read_spool(claimed), claimed)
    except SpoolFileError as exc:
        _quarantine(claimed, str(exc))
        return

    queues = _queues()
    for index, entry in enumerate(entries):
        try:
            await queues[entry["queue"]].enqueue(entry["item"])
        except Exception as exc:
            remaining = entries[index:]
            _write_atomically(SPOOL_PATH, remaining + _existing_entries())
            claimed.unlink()
            logger.error(
                "Re-queued %s of %s saved notification(s); the other %s (%s) went back to %s after: %s",
                index,
                len(entries),
                len(remaining),
                _summary(remaining),
                SPOOL_PATH,
                exc,
            )
            return
    claimed.unlink()
    logger.warning("Re-queued %s notification(s) (%s) saved at the last shutdown", len(entries), _summary(entries))


async def restore_notification_backlog() -> None:
    leftovers = _unfinished_restores()
    if leftovers and is_multi_worker():
        logger.error(
            "Found %s interrupted notification restore file(s) (%s); another worker may still own them, "
            "so they are left in place for the operator",
            len(leftovers),
            ", ".join(str(path) for path in leftovers),
        )
        leftovers = []

    for path in [*leftovers, SPOOL_PATH]:
        if not path.exists():
            continue
        claimed = _claim(path)
        if claimed is None:
            continue
        try:
            await _restore_file(claimed)
        except Exception:
            logger.exception(
                "Restoring saved notifications from %s failed; the file is kept for the next start", claimed
            )
