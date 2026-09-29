import asyncio
import random
from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import certifi
import pytest
from PasarGuardNodeBridge.common.service_pb2 import User as ProtoUser
from sqlalchemy import select

from app import notification
from app.db.models import User, UserStatus
from app.fork.jobs import status_review
from app.jobs import review_users
from app.models.user import BulkUsersSelection, UserModify
from app.node import bridge as node_bridge, node_manager, sync as node_sync
from app.node.bridge import QueuedGrpcNode
from app.operation import OperatorType
from app.operation.user import UserOperation
from tests.a1_bridge_harness import RecordingNode, TestGetDB, isolate_node_manager, seed_group, seed_user
from tests.api import TestSession
from tests.api.helpers import unique_name

OWNER = SimpleNamespace(username="owner", is_owner=True, role=None, id=None)


class CurrentStateNode(RecordingNode):
    def __init__(self, node_id: int):
        super().__init__(node_id)
        self._user_sync_resolver = node_sync.refresh_node_users


def silence_side_effects(monkeypatch):
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in ("remove_user", "modify_user", "user_status_change", "create_user"):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(node_sync, "GetDB", TestGetDB)
    monkeypatch.setattr(review_users, "GetDB", TestGetDB)
    monkeypatch.setattr(status_review, "GetDB", TestGetDB)


class ReaderBehindWriters(node_sync.CurrentStateReader):
    def __init__(self, writers: asyncio.Lock):
        super().__init__()
        self._writers = writers

    async def _query(self, user_ids):
        async with self._writers:
            return await super()._query(user_ids)


async def drained(node, timeout=15.0):
    async with asyncio.timeout(timeout):
        while True:
            await asyncio.sleep(0.2)
            if not await node._user_sync_store.has_pending(node.node_id):
                await asyncio.sleep(0.3)
                if not await node._user_sync_store.has_pending(node.node_id):
                    return


async def test_a_bulk_push_goes_through_the_per_user_queue_even_without_the_shared_protocol(monkeypatch):
    node = RecordingNode(3101)
    await node.connect("0.6.4", "26.3.27")
    isolate_node_manager(monkeypatch, node_manager, {3101: node})
    queued = []
    original = node.update_users

    async def record(users):
        queued.append([user.email for user in users])
        await original(users)

    monkeypatch.setattr(node, "update_users", record)
    await node_manager._update_users([ProtoUser(email="1", inbounds=["a"]), ProtoUser(email="2", inbounds=[])])
    assert queued == [["1", "2"]]
    await drained(node)
    assert node.live.get("1") == ["a"]
    assert all(kind == "chunked" for kind, _, _ in node.deliveries)
    await node.disconnect()


async def test_a_panel_user_the_current_state_read_does_not_return_is_delivered_as_a_removal(monkeypatch):
    async def answers_nobody(node_id, emails):
        return []

    monkeypatch.setattr(node_bridge, "_refresh_handler", answers_nobody)
    node = QueuedGrpcNode(
        address="127.0.0.1",
        port=1,
        api_port=1,
        server_ca=certifi.contents(),
        api_key=str(uuid4()),
        node_id="fallback",
    )
    try:
        stale = [ProtoUser(email="41", inbounds=["stale"]), ProtoUser(email="not-a-panel-user", inbounds=["kept"])]
        current = await node._current_state(stale)
        assert [(user.email, list(user.inbounds)) for user in current] == [
            ("41", []),
            ("not-a-panel-user", ["kept"]),
        ]
    finally:
        node.channel.close()
        await node._json_client.close()


@pytest.mark.parametrize("node_class", [RecordingNode, CurrentStateNode], ids=["queued-payload", "fresh-read"])
async def test_every_push_path_racing_the_worker_converges_to_the_database(monkeypatch, node_class):
    silence_side_effects(monkeypatch)
    operator = UserOperation(OperatorType.API)
    failures = []
    for round_number in range(8):
        rng = random.Random(round_number)
        tag = unique_name("one_channel")
        group_id = await seed_group(tag)
        ids = [await seed_user(group_id) for _ in range(8)]
        hold_ids = [
            await seed_user(
                group_id,
                status=UserStatus.on_hold,
                on_hold_expire_duration=86400,
                on_hold_timeout=dt.now(UTC) - td(minutes=5),
                created_at=dt.now(UTC) - td(days=2),
            )
            for _ in range(3)
        ]
        node = node_class(3200 + round_number)
        node._sync_poll_interval = rng.choice((0.01, 0.2, 1.0))
        await node.connect("0.6.4", "26.3.27")
        node.live.update({str(user_id): [tag] for user_id in ids + hold_ids})
        isolate_node_manager(monkeypatch, node_manager, {3200 + round_number: node})

        one_writer = asyncio.Lock()
        monkeypatch.setattr(node_sync, "_current_state", ReaderBehindWriters(one_writer))

        async def edit(user_id, note, one_writer=one_writer):
            async with one_writer, TestSession() as db:
                await operator.modify_user_by_id(db, user_id, UserModify(note=note), OWNER)

        async def delete(user_id, one_writer=one_writer):
            async with one_writer, TestSession() as db:
                await operator.remove_user_by_id(db, user_id, OWNER)

        async def bulk_delete(user_ids, one_writer=one_writer):
            async with one_writer, TestSession() as db:
                await operator.bulk_remove_users(db, BulkUsersSelection(ids=set(user_ids)), OWNER)

        async def extend(user_id, one_writer=one_writer):
            async with one_writer, TestSession() as db:
                await operator.modify_user_by_id(
                    db, user_id, UserModify(on_hold_timeout=dt.now(UTC) + td(days=3)), OWNER
                )

        async def on_hold_pass(user_ids, one_writer=one_writer):
            async with one_writer:
                await status_review.remove_on_hold_users_under_lock(user_ids)

        victims = rng.sample(ids, 4)
        edited = [user_id for user_id in ids if user_id not in victims]
        extended = rng.sample(hold_ids, 2)

        async def writer(seed, edited=edited, round_number=round_number, edit=edit):
            local = random.Random(seed)
            for step in range(3):
                await edit(local.choice(edited), f"r{round_number}-{seed}-{step}")
                await asyncio.sleep(local.choice((0, 0.01, 0.05)))

        async def deleter(
            victims=victims, rng=rng, round_number=round_number, edit=edit, delete=delete, bulk_delete=bulk_delete
        ):
            await asyncio.sleep(rng.choice((0, 0.02, 0.1)))
            await edit(victims[0], f"r{round_number}-before-delete")
            await delete(victims[0])
            for victim in victims[1:]:
                await edit(victim, f"r{round_number}-before-bulk-delete")
            await bulk_delete(victims[1:])

        extend_first = extended[:1]
        extend_during_pass = set(extended[1:])
        real_remove = review_users.sync_remove_user_awaited

        async def admin_extends_mid_pass(response, extend_during_pass=extend_during_pass, real_remove=real_remove):
            if response.id in extend_during_pass:
                extend_during_pass.discard(response.id)
                async with TestSession() as db:
                    await operator.modify_user_by_id(
                        db, response.id, UserModify(on_hold_timeout=dt.now(UTC) + td(days=3)), OWNER
                    )
            await real_remove(response)

        monkeypatch.setattr(review_users, "sync_remove_user_awaited", admin_extends_mid_pass)

        async def hold_pass(
            rng=rng, extend_first=extend_first, hold_ids=hold_ids, extend=extend, on_hold_pass=on_hold_pass
        ):
            await asyncio.sleep(rng.choice((0, 0.02, 0.1)))
            for user_id in extend_first:
                await extend(user_id)
            await on_hold_pass(hold_ids)

        try:
            await asyncio.gather(writer(round_number * 10 + 1), writer(round_number * 10 + 2), deleter(), hold_pass())
            await drained(node)
            async with TestSession() as session:
                rows = (await session.execute(select(User.id, User.status).where(User.id.in_(ids + hold_ids)))).all()
            present = {str(row.id) for row in rows if row.status in (UserStatus.active, UserStatus.on_hold)}
            on_node = {email for email in node.live if email in {str(user_id) for user_id in ids + hold_ids}}
            if on_node != present:
                failures.append(
                    f"round {round_number}: node holds {sorted(on_node)} but the database has {sorted(present)} "
                    f"(deliveries: {node.deliveries[-40:]})"
                )
        except Exception as exc:
            failures.append(f"round {round_number}: {type(exc).__name__}: {exc}")
        finally:
            await node.disconnect()
            await asyncio.sleep(1.0)
    assert failures == []
