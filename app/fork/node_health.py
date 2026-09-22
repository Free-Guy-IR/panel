from dataclasses import dataclass

from PasarGuardNodeBridge import Health

from app.db import GetDB
from app.db.models import Node, NodeStatus
from app.operation.node import NodeOperation
from app.utils.logger import get_logger

USAGE_FAILURE_THRESHOLD = 10
USAGE_STALLED_PREFIX = "Usage collection failing"
MAX_ERROR_LENGTH = 512

logger = get_logger("node-health")


@dataclass
class _Streak:
    failures: int = 0
    last_error: str = ""


def _describe(error: object) -> str:
    text = str(error)
    kind = type(error).__name__
    if isinstance(error, BaseException) and not text.startswith(kind):
        text = f"{kind}: {text}" if text else kind
    return text[:MAX_ERROR_LENGTH]


class UsageCollectionTracker:
    def __init__(self, threshold: int = USAGE_FAILURE_THRESHOLD):
        self.threshold = threshold
        self._streaks: dict[int, _Streak] = {}

    def record_success(self, node_id: int | None) -> None:
        if node_id is None:
            return
        previous = self._streaks.get(node_id)
        if previous is not None and previous.failures >= self.threshold:
            logger.info(f"Usage collection for node {node_id} recovered after {previous.failures} failed attempts")
        self._streaks[node_id] = _Streak()

    def record_failure(self, node_id: int | None, error: object) -> None:
        if node_id is None:
            return
        streak = self._streaks.setdefault(node_id, _Streak())
        streak.failures += 1
        streak.last_error = _describe(error)

    def observed(self, node_id: int) -> bool:
        return node_id in self._streaks

    def failures(self, node_id: int) -> int:
        streak = self._streaks.get(node_id)
        return streak.failures if streak is not None else 0

    def is_stalled(self, node_id: int) -> bool:
        return self.failures(node_id) >= self.threshold

    def stalled_message(self, node_id: int) -> str:
        streak = self._streaks.get(node_id) or _Streak()
        return (
            f"{USAGE_STALLED_PREFIX}: {streak.failures} attempts in a row returned no stats, "
            f"so traffic on this node is not being billed. Last error: {streak.last_error}"
        )

    def clear(self) -> None:
        self._streaks.clear()


usage_collection = UsageCollectionTracker()


def record_usage_success(node_id: int | None) -> None:
    usage_collection.record_success(node_id)


def record_usage_failure(node_id: int | None, error: object) -> None:
    usage_collection.record_failure(node_id, error)


def _flagged_for_usage(db_node: Node) -> bool:
    return db_node.status == NodeStatus.error and (db_node.message or "").startswith(USAGE_STALLED_PREFIX)


async def hold_for_usage_collection(db_node: Node, health: Health) -> bool:
    if health is not Health.HEALTHY:
        return False
    flagged = _flagged_for_usage(db_node)
    if usage_collection.is_stalled(db_node.id):
        if not flagged:
            async with GetDB() as db:
                await NodeOperation._update_single_node_status(
                    db, db_node.id, NodeStatus.error, message=usage_collection.stalled_message(db_node.id)
                )
        return True
    return flagged and not usage_collection.observed(db_node.id)
