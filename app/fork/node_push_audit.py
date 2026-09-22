from app.utils.logger import get_logger

logger = get_logger("node-push-audit")

SIGNIFICANT_DROP_RATIO = 0.95
SMALLEST_COUNT_WORTH_WATCHING = 20

_last_pushed_count: dict[int, int] = {}


def record_user_push(node_id: int, count: int, node_name: str | None = None) -> None:
    previous = _last_pushed_count.get(node_id)
    _last_pushed_count[node_id] = count

    if previous is None or previous < SMALLEST_COUNT_WORTH_WATCHING:
        return
    if count >= previous * SIGNIFICANT_DROP_RATIO:
        return

    label = node_name or f"node {node_id}"
    dropped = previous - count
    logger.warning(
        f'[{label}] user push fell from {previous} to {count} ({dropped} fewer, '
        f"{dropped / previous:.1%}); those users lose service on this node until the next push restores them"
    )


def forget_node(node_id: int) -> None:
    _last_pushed_count.pop(node_id, None)


def last_pushed_count(node_id: int) -> int | None:
    return _last_pushed_count.get(node_id)


def reset() -> None:
    _last_pushed_count.clear()
