from app.fork.operation.node_extras import _connect_lock

_generations: dict[int, int] = {}


def connect_generation(node_id: int) -> int:
    return _generations.get(node_id, 0)


def bump_connect_generation(node_id: int) -> None:
    _generations[node_id] = _generations.get(node_id, 0) + 1


def superseded(node_id: int, generation: int) -> bool:
    return connect_generation(node_id) != generation or _connect_lock(node_id).locked()
