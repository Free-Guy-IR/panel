import contextlib

from PasarGuardNodeBridge.common import service_pb2 as service
from sqlalchemy import delete, select

from app.fork import node_removals
from app.fork.crud.node_removals import due_node_removals, mark_node_removals
from app.fork.models.pending_node_removal import PendingNodeRemoval
from app.node import node_manager
from tests.api import GetTestDB, TestSession
from tests.test_node_connect_stale_snapshot import Harness, eventually

REFUSAL = "failed to enqueue NATS KV key after CAS retries"
FIRST_ID = 10_000_000
ROUNDS = 3


class Node:
    def __init__(self, refusing: bool = False):
        self.refusing = refusing
        self.removed: set[str] = set()
        self.before_refusal = None

    async def update_users(self, users):
        if self.refusing:
            if self.before_refusal is not None:
                hook, self.before_refusal = self.before_refusal, None
                await hook()
            raise RuntimeError(REFUSAL)
        self.removed.update(user.email for user in users if not list(user.inbounds))


async def pending_removals(ids: list[int]) -> set[str]:
    async with TestSession() as session:
        await session.execute(delete(PendingNodeRemoval))
        await mark_node_removals(session, ids)
        await session.commit()
    async with TestSession() as session:
        due = await due_node_removals(session, len(ids) + 1)
    assert [user_id for user_id, _ in due] == ids
    return {str(user_id) for user_id in ids}


async def pending_removals_beyond_one_chunk() -> set[str]:
    return await pending_removals(list(range(FIRST_ID, FIRST_ID + node_removals.DRAIN_CHUNK + 5)))


async def mark_again(ids: list[int]) -> None:
    async with TestSession() as session:
        await mark_node_removals(session, ids)
        await session.commit()


async def still_pending(emails: set[str]) -> set[str]:
    async with TestSession() as session:
        rows = (await session.execute(select(PendingNodeRemoval.user_id))).scalars().all()
    return {str(user_id) for user_id in rows} & emails


async def drain_rounds(rounds: int = ROUNDS) -> None:
    for _ in range(rounds):
        with contextlib.suppress(RuntimeError):
            await node_removals.drain_node_removals()


def first_chunk(emails: set[str]) -> set[str]:
    return set(sorted(emails, key=int)[: node_removals.DRAIN_CHUNK])


async def test_every_pending_removal_reaches_every_node_when_all_of_them_accept(monkeypatch):
    monkeypatch.setattr(node_removals, "GetDB", GetTestDB)
    emails = await pending_removals_beyond_one_chunk()
    nodes = {1: Node(), 2: Node()}
    monkeypatch.setattr(node_manager, "_nodes", nodes)

    await drain_rounds(1)

    assert all(node.removed >= emails for node in nodes.values())
    assert await still_pending(emails) == set()


async def test_a_refusing_node_does_not_hold_back_removals_past_the_first_chunk_from_the_healthy_nodes(monkeypatch):
    monkeypatch.setattr(node_removals, "GetDB", GetTestDB)
    emails = await pending_removals_beyond_one_chunk()
    healthy, refusing = Node(), Node(refusing=True)
    monkeypatch.setattr(node_manager, "_nodes", {1: refusing, 2: healthy})

    await drain_rounds()

    assert healthy.removed >= first_chunk(emails)
    missed = emails - healthy.removed
    assert not missed, (
        f"one node refused every push, and after {ROUNDS} drain rounds {len(missed)} of {len(emails)} pending removals "
        f"(user ids from {min(missed, key=int)}) never reached the healthy node: the round stops at the first chunk "
        f"of {node_removals.DRAIN_CHUNK} a node refuses"
    )

    refusing.refusing = False
    await drain_rounds()

    missed_by_the_recovered_node = emails - refusing.removed
    assert not missed_by_the_recovered_node, (
        f"the node accepts pushes again but {len(missed_by_the_recovered_node)} pending removals never reached it"
    )
    assert await still_pending(emails) == set()


async def test_a_bridge_node_whose_queue_refuses_does_not_hold_back_removals_from_a_healthy_bridge_node(monkeypatch):
    monkeypatch.setattr(node_removals, "GetDB", GetTestDB)
    async with Harness(monkeypatch) as h:
        refusing_node, healthy_node = h.node(1), h.node(1)
        refusing_pg = await h.boot(refusing_node)
        await h.boot(healthy_node)
        emails = await pending_removals_beyond_one_chunk()
        for db_node in (refusing_node, healthy_node):
            carried = h.remote(db_node).backends[service.BackendType.XRAY]["users"]
            carried.update({email: (h.tag_a,) for email in emails})

        store = refusing_pg._user_sync_store
        real_enqueue = store.enqueue_users
        refusing = [True]

        async def one_node_refuses(node_id, users):
            if refusing[0] and node_id == str(refusing_node.id):
                raise RuntimeError(f"failed to enqueue NATS KV key=p.{node_id} after CAS retries")
            return await real_enqueue(node_id, users)

        monkeypatch.setattr(store, "enqueue_users", one_node_refuses)

        def still_served_by(db_node) -> set[str]:
            return set(h.remote(db_node).backends[service.BackendType.XRAY]["users"]) & emails

        await drain_rounds()

        assert await eventually(lambda: not still_served_by(healthy_node), timeout=10.0), (
            f"one bridge node's queue refused every push, and after {ROUNDS} drain rounds the healthy node still "
            f"serves {len(still_served_by(healthy_node))} of {len(emails)} deleted users"
        )
        assert still_served_by(refusing_node) == emails
        assert await still_pending(emails) == emails

        refusing[0] = False
        await drain_rounds()

        assert await eventually(lambda: not still_served_by(refusing_node), timeout=10.0), (
            f"the node's queue accepts again but it still serves {len(still_served_by(refusing_node))} deleted users"
        )
        assert await still_pending(emails) == set()


async def test_removals_recorded_during_a_refused_round_reach_the_healthy_node_and_stay_owed_to_the_refusing_one(
    monkeypatch,
):
    monkeypatch.setattr(node_removals, "GetDB", GetTestDB)
    ids = list(range(FIRST_ID, FIRST_ID + node_removals.DRAIN_CHUNK + 5))
    emails = await pending_removals(ids)
    healthy, refusing = Node(), Node(refusing=True)
    monkeypatch.setattr(node_manager, "_nodes", {1: refusing, 2: healthy})
    behind_the_cursor = FIRST_ID - 1
    ahead_of_the_cursor = ids[-1] + node_removals.DRAIN_CHUNK
    re_recorded = ids[3]

    async def record_more_removals_while_the_first_chunk_is_in_flight():
        await mark_again([behind_the_cursor, re_recorded, ahead_of_the_cursor])

    refusing.before_refusal = record_more_removals_while_the_first_chunk_is_in_flight
    every_email = emails | {str(behind_the_cursor), str(ahead_of_the_cursor)}

    await drain_rounds()

    missed = every_email - healthy.removed
    assert not missed, (
        f"{len(missed)} removals recorded before or during a round one node refused never reached the healthy node: "
        f"{sorted(missed, key=int)[:5]}"
    )
    assert await still_pending(every_email) == every_email

    refusing.refusing = False
    await drain_rounds(1)

    assert every_email <= refusing.removed, sorted(every_email - refusing.removed, key=int)[:5]
    assert await still_pending(every_email) == set()
