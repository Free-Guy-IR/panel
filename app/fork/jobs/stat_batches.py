import inspect
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime as dt
from typing import NamedTuple

from sqlalchemy import delete, insert, select, update

from app.fork.models.node_stat_ack import NodeStatAck

TWO_PHASE_KEYWORDS = ("hold", "ack_epoch", "ack_batch_id")


class StatBatch(NamedTuple):
    epoch: str
    batch_id: int
    billable: bool = True


class StaleStatBatches(Exception):
    def __init__(self, node_ids: Iterable[int]):
        self.node_ids = frozenset(node_ids)
        super().__init__(f"stats batches already billed for node(s) {sorted(self.node_ids)}")


_two_phase_by_class: dict[type, bool] = {}
_epoch_by_node: dict[tuple[int, int], str] = {}


def supports_two_phase(node) -> bool:
    get_stats = getattr(node, "get_stats", None)
    if get_stats is None:
        return False
    node_class = type(node)
    known = _two_phase_by_class.get(node_class)
    if known is None:
        try:
            parameters = inspect.signature(get_stats).parameters
        except TypeError, ValueError:
            parameters = {}
        known = all(keyword in parameters for keyword in TWO_PHASE_KEYWORDS)
        _two_phase_by_class[node_class] = known
    return known


def request_keywords(ack: StatBatch | None) -> dict:
    return {
        "hold": True,
        "ack_epoch": ack.epoch if ack is not None else "",
        "ack_batch_id": ack.batch_id if ack is not None else 0,
    }


def batch_from_response(response) -> StatBatch | None:
    epoch = getattr(response, "batch_epoch", "") or ""
    if not isinstance(epoch, str) or not epoch:
        return None
    return StatBatch(epoch, int(getattr(response, "batch_id", 0) or 0))


def remember_epoch(node_id: int, stat_type: int, batch: StatBatch) -> None:
    _epoch_by_node[(int(node_id), int(stat_type))] = batch.epoch


def held(batches: Mapping[int, StatBatch] | None) -> dict[int, StatBatch]:
    return {node_id: batch for node_id, batch in (batches or {}).items() if batch.batch_id > 0}


def as_ack_only(batches: Mapping[int, StatBatch], node_ids: Iterable[int]) -> dict[int, StatBatch]:
    stale = set(node_ids)
    return {
        node_id: batch._replace(billable=False) if node_id in stale else batch for node_id, batch in batches.items()
    }


async def load_acks(db, stat_type: int, node_ids: Iterable[int]) -> dict[int, StatBatch]:
    ids = sorted({int(node_id) for node_id in node_ids})
    if not ids:
        return {}
    stat_type = int(stat_type)
    known = {node_id: _epoch_by_node[(node_id, stat_type)] for node_id in ids if (node_id, stat_type) in _epoch_by_node}
    matches = NodeStatAck.node_id.in_(ids)
    if known:
        matches = matches | NodeStatAck.batch_epoch.in_(sorted(set(known.values())))
    rows = (
        await db.execute(
            select(NodeStatAck.node_id, NodeStatAck.batch_epoch, NodeStatAck.batch_id, NodeStatAck.updated_at).where(
                NodeStatAck.stat_type == stat_type, matches
            )
        )
    ).all()
    by_epoch = {epoch: int(batch_id) for _, epoch, batch_id, _ in rows}
    latest: dict[int, tuple] = {}
    for node_id, epoch, batch_id, updated_at in rows:
        if node_id not in ids:
            continue
        current = latest.get(node_id)
        if current is None or (updated_at, batch_id) > (current[2], current[1]):
            latest[node_id] = (epoch, int(batch_id), updated_at)
    acks = {node_id: StatBatch(epoch, batch_id) for node_id, (epoch, batch_id, _) in latest.items()}
    for node_id, epoch in known.items():
        if epoch in by_epoch:
            acks[node_id] = StatBatch(epoch, by_epoch[epoch])
    return acks


async def settle_batches(conn, stat_type: int, batches: Mapping[int, StatBatch] | None) -> None:
    live = held(batches)
    if not live:
        return
    stat_type = int(stat_type)
    epochs = sorted({batch.epoch for batch in live.values()})
    stored = {
        epoch: int(batch_id)
        for epoch, batch_id in (
            await conn.execute(
                select(NodeStatAck.batch_epoch, NodeStatAck.batch_id)
                .where(NodeStatAck.stat_type == stat_type, NodeStatAck.batch_epoch.in_(epochs))
                .order_by(NodeStatAck.batch_epoch)
                .with_for_update()
            )
        ).all()
    }

    carried: dict[str, int] = {}
    stale: set[int] = set()
    for node_id, batch in sorted(live.items()):
        already = max(stored.get(batch.epoch, 0), carried.get(batch.epoch, 0))
        if batch.billable and batch.batch_id <= already:
            stale.add(node_id)
        carried[batch.epoch] = max(carried.get(batch.epoch, 0), batch.batch_id)
    if stale:
        raise StaleStatBatches(stale)

    now = dt.now(UTC)
    for node_id, batch in sorted(live.items()):
        if batch.epoch in stored:
            await conn.execute(
                update(NodeStatAck)
                .where(NodeStatAck.stat_type == stat_type, NodeStatAck.batch_epoch == batch.epoch)
                .values(node_id=node_id, batch_id=max(stored[batch.epoch], batch.batch_id), updated_at=now)
                .execution_options(synchronize_session=False)
            )
            stored[batch.epoch] = max(stored[batch.epoch], batch.batch_id)
        else:
            await conn.execute(
                insert(NodeStatAck).values(
                    batch_epoch=batch.epoch,
                    stat_type=stat_type,
                    node_id=node_id,
                    batch_id=batch.batch_id,
                    updated_at=now,
                )
            )
            stored[batch.epoch] = batch.batch_id
        await conn.execute(
            delete(NodeStatAck)
            .where(
                NodeStatAck.stat_type == stat_type,
                NodeStatAck.node_id == node_id,
                NodeStatAck.batch_epoch != batch.epoch,
            )
            .execution_options(synchronize_session=False)
        )
