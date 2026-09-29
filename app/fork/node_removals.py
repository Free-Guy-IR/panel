import asyncio

from PasarGuardNodeBridge.common.service_pb2 import User as ProtoUser

from app.db import GetDB
from app.fork.crud.node_removals import clear_node_removals, due_node_removals, existing_user_ids
from app.node import node_manager
from app.node.user import _serialize_user_for_node
from app.utils.logger import get_logger

DRAIN_CHUNK = 500

logger = get_logger("node-removals")


async def _enqueue_on_every_node(users: list[ProtoUser]) -> None:
    nodes = list((await node_manager.get_nodes()).items())
    results = await asyncio.gather(*(node.update_users(users) for _, node in nodes), return_exceptions=True)
    failed = [(node_id, result) for (node_id, _), result in zip(nodes, results) if isinstance(result, BaseException)]
    if failed:
        node_id, error = failed[0]
        raise RuntimeError(
            f"{len(failed)} node queue(s) refused {len(users)} removal(s), first node {node_id}: {error}"
        )


async def drain_node_removals() -> int:
    drained = 0
    after_user_id = 0
    while True:
        async with GetDB() as db:
            removals = await due_node_removals(db, DRAIN_CHUNK, after_user_id=after_user_id)
            if not removals:
                return drained
            after_user_id = removals[-1][0]
            back = await existing_user_ids(db, [user_id for user_id, _ in removals])
        gone = [_serialize_user_for_node(user_id, {}) for user_id, _ in removals if user_id not in back]
        if gone:
            await _enqueue_on_every_node(gone)
        async with GetDB() as db:
            await clear_node_removals(db, removals)
            await db.commit()
        drained += len(removals)
