import asyncio
import contextlib
import copy
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime as dt, timedelta as td
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import certifi
from fastapi import HTTPException
from grpclib.client import Channel
from grpclib.const import Cardinality, Handler
from grpclib.server import Server
from PasarGuardNodeBridge.common import service_grpc, service_pb2 as service
from sqlalchemy import select, tuple_, update

from app import notification
from app.db.crud.admin import get_admin_by_id
from app.db.models import Admin, Group, ProxyInbound, User, UserStatus, users_groups_association
from app.fork import node_removals
from app.fork.jobs import connection_limiter
from app.fork.models.connection import ConnectionRestriction
from app.fork.models.proxy_secret_claim import ProxySecretClaim
from app.fork.proxy_secrets import group_repair
from app.fork.proxy_secrets.claims import secret_digest
from app.fork.proxy_secrets.fields import ProxySecretField
from app.fork.proxy_secrets.locks import is_deadlock
from app.models.admin import AdminDetails, AdminRoleData
from app.models.group import GroupModify
from app.models.proxy import ProxyTable
from app.models.settings import ConnectionLimit
from app.models.user import UserModify
from app.node import bridge as node_bridge, node_manager, sync as node_sync
from app.node.bridge import QueuedGrpcNode
from app.node.user import _serialize_user_for_node, core_users
from app.operation import OperatorType, user as user_operation
from app.operation.admin import AdminOperation
from app.operation.group import GroupOperation
from app.operation.user import UserOperation
from tests.a1_bridge_harness import TestGetDB, isolate_node_manager
from tests.api import IS_SQLITE, TestSession
from tests.api.helpers import OPERATOR_ROLE_ID, unique_name

OWNER = AdminDetails(id=None, username="owner", role=AdminRoleData(id=1, name="owner", is_owner=True))
LIMITS = ConnectionLimit(
    enabled=True,
    monitor_only=False,
    enforcement_enabled=True,
    persistence_cycles=1,
    punishment_steps=[30],
)
NODE_CREDENTIALS = {
    "vmess": ("id",),
    "vless": ("id",),
    "trojan": ("password",),
    "shadowsocks": ("password",),
    "wireguard": ("public_key",),
    "hysteria": ("auth",),
    "hysteria2": ("password",),
    "open_vpn": ("password",),
    "mtproto": ("secret",),
    "tuic": ("uuid", "password"),
    "l2tp": ("password",),
}
ROTATED_BY_A_REVOKE = (
    ("vmess", "id"),
    ("vless", "id"),
    ("trojan", "password"),
    ("shadowsocks", "password"),
    ("hysteria", "auth"),
    ("hysteria2", "password"),
    ("tuic", "uuid"),
    ("tuic", "password"),
)
SUBMITTED_BY_ANOTHER_USER = (("vless", "id"), ("trojan", "password"), ("hysteria", "auth"))
DISPATCH_DELAYS = (0, 0, 0.01, 0.05, 0.3)
SERVER_DATABASE = not IS_SQLITE
FREE_RUNNING_ROUNDS = 10
CLIENT_RETRIES = 5


class NodeReceiver:
    def __init__(self):
        self.live: dict[str, service.User] = {}
        self.deliveries: list[service.User] = []

    async def chunked(self, stream):
        users = [user for chunk in [chunk async for chunk in stream] for user in chunk.users]
        for user in users:
            delivered = service.User()
            delivered.CopyFrom(user)
            self.deliveries.append(delivered)
            if delivered.inbounds:
                self.live[delivered.email] = delivered
            else:
                self.live.pop(delivered.email, None)
        await stream.send_message(service.Empty())

    def __mapping__(self):
        return {
            "/service.NodeService/SyncUsersChunked": Handler(
                self.chunked, Cardinality.STREAM_UNARY, service.UsersChunk, service.Empty
            )
        }


class Schedule:
    def __init__(self, steps: list[str] | None, rng: random.Random):
        self.steps = steps
        self.rng = rng
        self.position = 0
        self.retries: list[str] = []
        self.turn = asyncio.Condition()
        self.writing = asyncio.Lock()

    async def reached(self, name: str) -> None:
        if self.steps is None:
            return
        async with self.turn:
            await asyncio.wait_for(self.turn.wait_for(lambda: self.steps[self.position] == name), 60)

    @contextlib.asynccontextmanager
    async def step(self, name: str):
        if self.steps is None:
            await asyncio.sleep(self.rng.choice((0, 0.005, 0.02, 0.06)))
            yield
            return
        async with self.turn:
            await asyncio.wait_for(self.turn.wait_for(lambda: self.steps[self.position] == name), 60)
        try:
            async with self.writing:
                yield
        finally:
            async with self.turn:
                self.position += 1
                self.turn.notify_all()


class ReaderBehindTheWritingStep(node_sync.CurrentStateReader):
    def __init__(self, schedule: Schedule):
        super().__init__()
        self._schedule = schedule

    async def _query(self, user_ids):
        if self._schedule.steps is None:
            return await super()._query(user_ids)
        async with self._schedule.writing:
            return await super()._query(user_ids)


class DelayedDispatch:
    def __init__(self, monkeypatch, rng: random.Random):
        self.rng = rng
        self.pending = 0
        self.real_one = node_sync._dispatch_user_update
        self.real_many = node_sync._dispatch_users_update
        monkeypatch.setattr(node_sync, "_dispatch_user_update", self.one)
        monkeypatch.setattr(node_sync, "_dispatch_users_update", self.many)

    def one(self, proto_user):
        self.pending += 1
        return self._later(self.real_one, proto_user)

    def many(self, proto_users):
        self.pending += 1
        return self._later(self.real_many, proto_users)

    async def _later(self, send, payload):
        try:
            await asyncio.sleep(self.rng.choice(DISPATCH_DELAYS))
            await send(payload)
        finally:
            self.pending -= 1

    async def settled(self):
        async with asyncio.timeout(30):
            while self.pending:
                await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)


@dataclass
class World:
    admin_id: int
    group_id: int
    group_name: str
    tags: tuple[str, str]
    target: int
    peer: int
    initial: dict[int, dict] = field(default_factory=dict)


@dataclass
class Outcome:
    value: object = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def refused_with(self, *codes: int) -> bool:
        return isinstance(self.error, HTTPException) and self.error.status_code in codes

    def __repr__(self) -> str:
        if self.error is None:
            return "ok"
        if isinstance(self.error, HTTPException):
            return f"HTTP {self.error.status_code}"
        return f"{type(self.error).__name__}: {self.error}"


def quiet(monkeypatch):
    for module in (node_sync, connection_limiter, node_removals, group_repair):
        monkeypatch.setattr(module, "GetDB", TestGetDB)
    monkeypatch.setattr(UserOperation, "generate_subscription_url", staticmethod(AsyncMock(return_value="")))
    for name in (
        "user_status_change",
        "modify_user",
        "modify_group",
        "remove_user",
        "user_subscription_revoked",
        "create_user",
    ):
        monkeypatch.setattr(notification, name, AsyncMock())
    monkeypatch.setattr(node_bridge, "_refresh_handler", node_sync.refresh_node_users)
    monkeypatch.setattr(group_repair, "IN_REQUEST_RETRY_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(group_repair, "REPAIR_DELAYS_SECONDS", (0.05, 0.1, 0.2))

    async def known_tags(self, tags):
        return None

    monkeypatch.setattr(GroupOperation, "check_inbound_tags", known_tags)


def interleaving(rng: random.Random, actors: dict[str, bool]) -> list[str]:
    steps = [
        f"{name}:{phase}" for name, staged in actors.items() for phase in (("load", "write") if staged else ("write",))
    ]
    rng.shuffle(steps)
    for name, staged in actors.items():
        if staged:
            load, write = steps.index(f"{name}:load"), steps.index(f"{name}:write")
            if load > write:
                steps[load], steps[write] = steps[write], steps[load]
    return steps


def worth_retrying(outcome: Outcome) -> bool:
    return outcome.refused_with(503) or is_deadlock(outcome.error)


async def retried_by_the_client(schedule: Schedule, name: str, attempt) -> Outcome:
    outcome = await attempt()
    if schedule.steps is not None:
        return outcome
    for _ in range(CLIENT_RETRIES):
        if not worth_retrying(outcome):
            break
        schedule.retries.append(f"{name}: {outcome!r}")
        await asyncio.sleep(schedule.rng.choice((0.05, 0.1, 0.2)))
        outcome = await attempt()
    return outcome


async def staged(schedule: Schedule, name: str, load, write) -> Outcome:
    return await retried_by_the_client(schedule, name, lambda: staged_once(schedule, name, load, write))


async def unstaged(schedule: Schedule, name: str, write) -> Outcome:
    return await retried_by_the_client(schedule, name, lambda: unstaged_once(schedule, name, write))


async def staged_once(schedule: Schedule, name: str, load, write) -> Outcome:
    db = TestSession()
    loaded = None
    failure = None
    async with schedule.step(f"{name}:load"):
        try:
            loaded = await load(db)
        except Exception as exc:
            failure = exc
    async with schedule.step(f"{name}:write"):
        try:
            if failure is not None:
                return Outcome(error=failure)
            return Outcome(value=await write(db, loaded))
        except Exception as exc:
            return Outcome(error=exc)
        finally:
            await db.close()


async def unstaged_once(schedule: Schedule, name: str, write) -> Outcome:
    async with schedule.step(f"{name}:write"):
        db = TestSession()
        try:
            return Outcome(value=await write(db))
        except Exception as exc:
            return Outcome(error=exc)
        finally:
            await db.close()


async def seed_world(group_tags: int = 1) -> World:
    tags = (unique_name("xilv_in"), unique_name("xilv_in"))
    async with TestSession() as db:
        admin = Admin(username=unique_name("xilv_admin"), hashed_password="secret", role_id=OPERATOR_ROLE_ID)
        inbounds = [ProxyInbound(tag=tag) for tag in tags]
        db.add_all([admin, *inbounds])
        await db.flush()
        group = Group(name=unique_name("xilv_group"), inbounds=inbounds[:group_tags])
        db.add(group)
        await db.flush()
        users = [
            User(
                username=unique_name("xilv_user"),
                admin_id=admin.id,
                proxy_settings=ProxyTable().dict(no_obj=True),
                status=UserStatus.active,
            )
            for _ in range(2)
        ]
        db.add_all(users)
        await db.flush()
        await db.execute(
            users_groups_association.insert(), [{"user_id": user.id, "groups_id": group.id} for user in users]
        )
        await db.commit()
        world = World(
            admin_id=admin.id,
            group_id=group.id,
            group_name=group.name,
            tags=tags,
            target=users[0].id,
            peer=users[1].id,
        )
    world.initial = {user_id: await stored_settings(user_id) for user_id in (world.target, world.peer)}
    return world


async def stored_settings(user_id: int) -> dict | None:
    async with TestSession() as db:
        return await db.scalar(select(User.proxy_settings).where(User.id == user_id))


async def stored_status(user_id: int) -> UserStatus | None:
    async with TestSession() as db:
        return await db.scalar(select(User.status).where(User.id == user_id))


async def committed_payloads(user_ids: list[int]) -> dict[str, service.User]:
    async with TestSession() as db:
        return {proto.email: proto for proto in await core_users(db, user_ids=user_ids)}


def node_view(proto: service.User | None):
    if proto is None:
        return None
    return proto.proxies.SerializeToString(deterministic=True), tuple(sorted(proto.inbounds))


def live_credentials(proto: service.User) -> set[tuple[str, str, str]]:
    found = set()
    for descriptor, section in proto.proxies.ListFields():
        for attribute in NODE_CREDENTIALS.get(descriptor.name, ()):
            value = getattr(section, attribute)
            if value:
                found.add((descriptor.name, attribute, value))
    return found


def empty_credentials(proto: service.User) -> list[str]:
    return [
        f"{descriptor.name}.{attribute}"
        for descriptor, section in proto.proxies.ListFields()
        for attribute in NODE_CREDENTIALS.get(descriptor.name, ())
        if not getattr(section, attribute)
    ]


def stored_credentials(settings: dict | None, keys=ROTATED_BY_A_REVOKE) -> set[tuple[str, str, str]]:
    if not settings:
        return set()
    found = set()
    for protocol, attribute in keys:
        value = (settings.get(protocol) or {}).get(attribute)
        if value:
            found.add((protocol, attribute, str(value)))
    return found


def form_from(settings: dict, overrides: dict | None = None) -> dict:
    form = copy.deepcopy(settings)
    for (protocol, attribute), value in (overrides or {}).items():
        form.setdefault(protocol, {})[attribute] = value
    return form


def taken_from(settings: dict, keys=SUBMITTED_BY_ANOTHER_USER) -> dict:
    return {(protocol, attribute): settings[protocol][attribute] for protocol, attribute in keys}


async def claim_rows(credentials: set[tuple[str, str, str]]) -> dict[tuple[str, str, str], tuple]:
    wanted = {
        (f"{protocol}.{attribute}", secret_digest(ProxySecretField(f"{protocol}.{attribute}"), value)): (
            protocol,
            attribute,
            value,
        )
        for protocol, attribute, value in credentials
    }
    if not wanted:
        return {}
    table = ProxySecretClaim.__table__
    async with TestSession() as db:
        rows = (
            await db.execute(
                select(table.c.kind, table.c.digest, table.c.user_id, table.c.retired_at).where(
                    tuple_(table.c.kind, table.c.digest).in_(list(wanted))
                )
            )
        ).all()
    return {wanted[(kind, digest)]: (user_id, retired_at) for kind, digest, user_id, retired_at in rows}


async def claims_owned_by(user_id: int) -> int:
    async with TestSession() as db:
        return len((await db.execute(select(ProxySecretClaim.digest).where(ProxySecretClaim.user_id == user_id))).all())


async def drained(node, timeout: float = 30.0) -> None:
    async with asyncio.timeout(timeout):
        while True:
            await asyncio.sleep(0.1)
            if not await node._user_sync_store.has_pending(node.node_id):
                await asyncio.sleep(0.2)
                if not await node._user_sync_store.has_pending(node.node_id):
                    return


@contextlib.asynccontextmanager
async def production_node(monkeypatch, key: int):
    receiver = NodeReceiver()
    server = Server([receiver])
    await server.start("127.0.0.1", 0)
    port = server._server.sockets[0].getsockname()[1]
    node = QueuedGrpcNode(
        address="127.0.0.1",
        port=port,
        api_port=1,
        server_ca=certifi.contents(),
        api_key=str(uuid4()),
        node_id=f"xilv-{uuid4().hex[:8]}",
        sync_poll_interval=0.01,
    )
    node.channel.close()
    node.channel = Channel("127.0.0.1", port, ssl=False)
    node._client = service_grpc.NodeServiceStub(node.channel)
    try:
        await node.connect("0.6.22", "26.3.27")
        isolate_node_manager(monkeypatch, node_manager, {key: node})
        yield SimpleNamespace(node=node, receiver=receiver)
    finally:
        await node.disconnect()
        node.channel.close()
        await node._json_client.close()
        server.close()
        await server.wait_closed()


async def attach_committed_state(remote, user_ids: list[int]) -> None:
    committed = list((await committed_payloads(user_ids)).values())
    if committed:
        await remote.node.update_users(committed)
    await drained(remote.node)


async def load_user(db, user_id: int):
    return await UserOperation(OperatorType.API).get_validated_user_by_id(db, user_id, OWNER, scope_action="update")


def revoke_of(operator: UserOperation, user_id: int):
    async def write(db, loaded):
        return await operator._revoke_user_sub(db, loaded, OWNER)

    return lambda db: load_user(db, user_id), write


def modify_of(operator: UserOperation, user_id: int, change_factory):
    async def write(db, loaded):
        return await operator._modify_user(db, loaded, change_factory(), OWNER)

    return lambda db: load_user(db, user_id), write


def form_save_of(operator: UserOperation, user_id: int, form: dict):
    async def write(db, loaded):
        return await operator._modify_user(db, loaded, UserModify(proxy_settings=form, note="form save"), OWNER)

    return lambda db: load_user(db, user_id), write


def group_change_of(world: World, inbound_tags: list[str]):
    async def write(db):
        return await GroupOperation(OperatorType.API).modify_group(
            db, world.group_id, GroupModify(name=world.group_name, inbound_tags=inbound_tags), OWNER
        )

    return write


async def limiter_disables(db, user_id: int):
    observation = SimpleNamespace(
        user_id=user_id,
        verdict="over_limit",
        streak=5,
        devices=4,
        limit_applied=2,
        details={},
        reasons=["cross-branch"],
    )
    await connection_limiter._enforce(db, [observation], LIMITS)


async def limiter_releases(db, user_id: int):
    await db.execute(
        update(ConnectionRestriction)
        .where(ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(True))
        .values(restore_at=dt.now(UTC) - td(seconds=1))
    )
    await db.commit()
    await connection_limiter._release_due(db, LIMITS)


async def admin_wide(db, admin_id: int, action: str):
    db_admin = await get_admin_by_id(db, admin_id)
    await getattr(AdminOperation(OperatorType.API), action)(db, db_admin, OWNER)


STATUS_WRITERS = ("limiter_disable", "limiter_release", "admin_disable", "admin_activate")


async def prepare_status_writer(world: World, writer: str) -> None:
    if writer == "limiter_release":
        async with TestGetDB() as db:
            await limiter_disables(db, world.target)
    elif writer == "admin_activate":
        async with TestSession() as db:
            await db.execute(update(User).where(User.id == world.target).values(status=UserStatus.disabled))
            await db.commit()


def status_writer_of(world: World, writer: str):
    async def write(db):
        if writer == "limiter_disable":
            return await limiter_disables(db, world.target)
        if writer == "limiter_release":
            return await limiter_releases(db, world.target)
        if writer == "admin_disable":
            return await admin_wide(db, world.admin_id, "_disable_all_active_users_for_admin")
        return await admin_wide(db, world.admin_id, "_activate_all_disabled_users_for_admin")

    return write


ADMIN_CHANGES = {
    "activate": lambda: UserModify(status=UserStatus.active),
    "renew": lambda: UserModify(status=UserStatus.active, expire=dt.now(UTC) + td(days=30)),
    "disable": lambda: UserModify(status=UserStatus.disabled),
    "limit": lambda: UserModify(data_limit=50 * 1024**3),
}


async def converged(remote, user_ids: list[int]) -> list[str]:
    expected = await committed_payloads(user_ids)
    problems = []
    for user_id in user_ids:
        email = str(user_id)
        on_node, in_database = remote.receiver.live.get(email), expected.get(email)
        if node_view(on_node) != node_view(in_database):
            problems.append(
                f"user {user_id}: the node holds {summary(on_node)} but the committed row serializes to "
                f"{summary(in_database)} (status {await stored_status(user_id)})"
            )
    return problems


def summary(proto: service.User | None) -> str:
    if proto is None:
        return "nothing"
    return f"inbounds {sorted(proto.inbounds)} with credentials {sorted(live_credentials(proto))[:3]}..."


def empty_credential_pushes(remote) -> list[str]:
    return [
        f"user {proto.email} was pushed with empty credentials {missing}"
        for proto in remote.receiver.deliveries
        if (missing := empty_credentials(proto))
    ]


def shared_live_credentials(remote) -> list[str]:
    seen: dict[tuple[str, str, str], str] = {}
    problems = []
    for email, proto in remote.receiver.live.items():
        for credential in live_credentials(proto):
            if credential in seen and seen[credential] != email:
                problems.append(f"users {seen[credential]} and {email} are both served {credential[:2]}")
            seen[credential] = email
    return problems


def rotated_away_live(remote, rotated: set[tuple[str, str, str]]) -> list[str]:
    return [
        f"user {email} is served the rotated-away {protocol}.{attribute}"
        for email, proto in remote.receiver.live.items()
        for protocol, attribute, value in live_credentials(proto)
        if (protocol, attribute, value) in rotated
    ]


async def holders_in_database(user_ids: list[int], credentials: set[tuple[str, str, str]]) -> dict[int, set]:
    found = {}
    for user_id in user_ids:
        held = stored_credentials(await stored_settings(user_id)) & credentials
        if held:
            found[user_id] = held
    return found


async def run_mixed_writers_round(monkeypatch, round_number: int, plan: dict) -> list[str]:
    rng = random.Random(round_number)
    operator = UserOperation(OperatorType.API)
    writer, change, grow = plan["status"], plan["change"], plan["grow"]
    isolate_node_manager(monkeypatch, node_manager, {})
    world = await seed_world(group_tags=1 if grow else 2)
    first, second = world.tags
    await prepare_status_writer(world, writer)
    actors = {"revoke": True, "modify": True, "save": True, "foreign": True, "group": False, "status": False}
    steps = None if plan.get("free") else plan.get("steps") or interleaving(rng, actors)
    schedule = Schedule(steps, rng)
    monkeypatch.setattr(node_sync, "_current_state", ReaderBehindTheWritingStep(schedule))
    dispatch = DelayedDispatch(monkeypatch, rng)
    target_initial = world.initial[world.target]
    foreign_form = form_from(world.initial[world.peer], taken_from(target_initial))
    async with production_node(monkeypatch, 5100 + round_number) as remote:
        await attach_committed_state(remote, [world.target, world.peer])
        outcomes = dict(
            zip(
                ("revoke", "modify", "save", "foreign", "group", "status"),
                await asyncio.gather(
                    staged(schedule, "revoke", *revoke_of(operator, world.target)),
                    staged(schedule, "modify", *modify_of(operator, world.target, ADMIN_CHANGES[change])),
                    staged(schedule, "save", *form_save_of(operator, world.target, form_from(target_initial))),
                    staged(schedule, "foreign", *form_save_of(operator, world.peer, foreign_form)),
                    unstaged(schedule, "group", group_change_of(world, [first, second] if grow else [first])),
                    unstaged(schedule, "status", status_writer_of(world, writer)),
                ),
                strict=True,
            )
        )
        await dispatch.settled()
        await drained(remote.node)
        label = f"round {round_number} ({writer}, {change}, {'grow' if grow else 'shrink'}, steps {steps})"
        problems = [f"{label}: {problem}" for problem in await converged(remote, [world.target, world.peer])]
        legal = {
            "revoke": (503,),
            "modify": (503,),
            "save": (409, 503),
            "foreign": (409, 503),
            "group": (503,),
            "status": (),
        }
        for name, outcome in outcomes.items():
            if not outcome.ok and not outcome.refused_with(*legal[name]):
                problems.append(f"{label}: {name} ended {outcome!r}")
        if outcomes["foreign"].ok:
            problems.append(f"{label}: user {world.peer} was allowed to store user {world.target}'s credentials")
        rotated = stored_credentials(target_initial) if outcomes["revoke"].ok else set()
        problems += [f"{label}: {problem}" for problem in rotated_away_live(remote, rotated)]
        held = await holders_in_database([world.target, world.peer], rotated)
        if held:
            problems.append(f"{label}: rotated-away credentials are stored again: {held}")
        problems += [f"{label}: {problem}" for problem in empty_credential_pushes(remote)]
        problems += [f"{label}: {problem}" for problem in shared_live_credentials(remote)]
        if steps is not None and outcomes["revoke"].ok:
            save_before_revoke = steps.index("save:write") < steps.index("revoke:write")
            if outcomes["save"].ok != save_before_revoke:
                problems.append(
                    f"{label}: the stale form save ended {outcomes['save']!r} although it was written "
                    f"{'before' if save_before_revoke else 'after'} the revoke"
                )
        return problems


MODIFY_LAST_AFTER_A_DISABLE = [
    "revoke:load",
    "save:load",
    "foreign:load",
    "revoke:write",
    "group:write",
    "modify:load",
    "status:write",
    "modify:write",
    "save:write",
    "foreign:write",
]
REVOKE_LAST_AFTER_THE_STALE_SAVES = [
    "save:load",
    "foreign:load",
    "modify:load",
    "revoke:load",
    "status:write",
    "save:write",
    "foreign:write",
    "group:write",
    "modify:write",
    "revoke:write",
]
FIXED_MIXED_PLANS = [
    {"status": "limiter_disable", "change": "activate", "grow": True, "steps": MODIFY_LAST_AFTER_A_DISABLE},
    {"status": "admin_disable", "change": "renew", "grow": False, "steps": MODIFY_LAST_AFTER_A_DISABLE},
    {"status": "limiter_release", "change": "disable", "grow": True, "steps": REVOKE_LAST_AFTER_THE_STALE_SAVES},
    {"status": "admin_activate", "change": "limit", "grow": False, "steps": REVOKE_LAST_AFTER_THE_STALE_SAVES},
]


def mixed_plans() -> list[dict | None]:
    plans: list[dict | None] = list(FIXED_MIXED_PLANS)
    for seed in range(12):
        rng = random.Random(1000 + seed)
        plans.append(
            {
                "status": STATUS_WRITERS[seed % len(STATUS_WRITERS)],
                "change": sorted(ADMIN_CHANGES)[(seed // len(STATUS_WRITERS)) % len(ADMIN_CHANGES)],
                "grow": rng.random() < 0.5,
            }
        )
    if SERVER_DATABASE:
        plans += [
            {
                "status": STATUS_WRITERS[seed % len(STATUS_WRITERS)],
                "change": sorted(ADMIN_CHANGES)[seed % len(ADMIN_CHANGES)],
                "grow": seed % 2 == 0,
                "free": True,
            }
            for seed in range(FREE_RUNNING_ROUNDS)
        ]
    return plans


async def test_mixed_writers_leave_the_node_serving_exactly_the_committed_row(monkeypatch):
    quiet(monkeypatch)
    failures = []
    for round_number, plan in enumerate(mixed_plans()):
        failures += await run_mixed_writers_round(monkeypatch, round_number, plan)
    assert failures == [], "\n".join(failures)


async def run_delete_round(monkeypatch, round_number: int, plan: dict) -> list[str]:
    rng = random.Random(7000 + round_number)
    operator = UserOperation(OperatorType.API)
    grow = plan["grow"]
    isolate_node_manager(monkeypatch, node_manager, {})
    world = await seed_world(group_tags=1 if grow else 2)
    first, second = world.tags
    actors = {"revoke": True, "modify": True, "group": False, "delete": False}
    steps = None if plan.get("free") else plan.get("steps") or interleaving(rng, actors)
    if steps is not None and plan.get("lost"):
        steps = [step for step in steps if step != "delete:write"] + ["delete:write"]
    schedule = Schedule(steps, rng)
    monkeypatch.setattr(node_sync, "_current_state", ReaderBehindTheWritingStep(schedule))
    dispatch = DelayedDispatch(monkeypatch, rng)
    lost: list[list[str]] = []

    async def removal_lost_with_the_process(users):
        lost.append([str(user.id) for user in users])

    monkeypatch.setattr(
        user_operation,
        "sync_remove_users",
        removal_lost_with_the_process if plan.get("lost") else node_sync.remove_users,
    )
    target_initial = world.initial[world.target]
    stale_payload = _serialize_user_for_node(world.target, target_initial, [first, second])

    async def delete(db):
        return await operator.remove_user_by_id(db, world.target, OWNER)

    async def delete_after_earlier_pushes_landed(remote):
        if plan.get("lost"):
            await schedule.reached("delete:write")
            await dispatch.settled()
            await drained(remote.node)
        return await unstaged(schedule, "delete", delete)

    async with production_node(monkeypatch, 6100 + round_number) as remote:
        await attach_committed_state(remote, [world.target, world.peer])
        outcomes = dict(
            zip(
                ("revoke", "modify", "group", "delete"),
                await asyncio.gather(
                    staged(schedule, "revoke", *revoke_of(operator, world.target)),
                    staged(schedule, "modify", *modify_of(operator, world.target, ADMIN_CHANGES["renew"])),
                    unstaged(schedule, "group", group_change_of(world, [first, second] if grow else [first])),
                    delete_after_earlier_pushes_landed(remote),
                ),
                strict=True,
            )
        )
        await dispatch.settled()
        await drained(remote.node)
        await node_removals.drain_node_removals()
        await dispatch.settled()
        await drained(remote.node)
        label = f"round {round_number} ({'lost' if plan.get('lost') else 'dispatched'} removal, steps {steps})"
        problems = []
        if not outcomes["delete"].ok:
            problems.append(f"{label}: the delete ended {outcomes['delete']!r}")
        for name in ("revoke", "modify", "group"):
            outcome = outcomes[name]
            if not outcome.ok and not outcome.refused_with(404, 503) and not is_gone_row(outcome.error):
                problems.append(f"{label}: {name} ended {outcome!r}")
        if await stored_status(world.target) is not None:
            problems.append(f"{label}: user {world.target} still exists after its delete")
        if str(world.target) in remote.receiver.live:
            problems.append(
                f"{label}: the deleted user {world.target} is still served by the node after the removal drain"
            )
        await node_manager.update_user(stale_payload)
        await drained(remote.node)
        if str(world.target) in remote.receiver.live:
            problems.append(f"{label}: a late push of a copy read before the delete brought user {world.target} back")
        problems += [f"{label}: {problem}" for problem in await converged(remote, [world.peer])]
        problems += [f"{label}: {problem}" for problem in empty_credential_pushes(remote)]
        owned = await claims_owned_by(world.target)
        if owned:
            problems.append(f"{label}: {owned} claim row(s) still name the deleted user {world.target}")
        held = stored_credentials(target_initial)
        if outcomes["revoke"].ok:
            held |= stored_credentials(outcomes["revoke"].value.proxy_settings.dict(no_obj=True))
        rows = await claim_rows(held)
        missing = sorted(credential[:2] for credential in held if credential not in rows)
        if missing:
            problems.append(f"{label}: the deleted user's credentials {missing} left no tombstone claim")
        not_tombstoned = sorted(credential[:2] for credential, (holder, _) in rows.items() if holder is not None)
        if not_tombstoned:
            problems.append(f"{label}: claims {not_tombstoned} still name a holder after the delete")
        reuse = await asyncio.gather(
            staged(
                Schedule(None, rng),
                "reuse",
                *form_save_of(operator, world.peer, form_from(world.initial[world.peer], taken_from(target_initial))),
            )
        )
        if not reuse[0].refused_with(409):
            problems.append(f"{label}: another user submitting the deleted user's credentials ended {reuse[0]!r}")
        return problems


def is_gone_row(error: BaseException | None) -> bool:
    if type(error).__name__ in {"StaleDataError", "ObjectDeletedError", "NoResultFound"}:
        return True
    return type(error).__name__ == "InvalidRequestError" and "refresh" in str(error).lower()


def delete_plans() -> list[dict]:
    plans = [
        {
            "lost": True,
            "grow": True,
            "steps": ["revoke:load", "modify:load", "revoke:write", "group:write", "modify:write", "delete:write"],
        },
        {
            "lost": True,
            "grow": False,
            "steps": ["group:write", "modify:load", "revoke:load", "modify:write", "revoke:write", "delete:write"],
        },
        {
            "lost": False,
            "grow": True,
            "steps": ["revoke:load", "modify:load", "delete:write", "revoke:write", "modify:write", "group:write"],
        },
        {
            "lost": False,
            "grow": False,
            "steps": ["modify:load", "revoke:load", "group:write", "delete:write", "modify:write", "revoke:write"],
        },
    ]
    plans += [{"lost": seed % 2 == 0, "grow": seed % 3 != 0} for seed in range(10)]
    if SERVER_DATABASE:
        plans += [{"lost": False, "grow": seed % 2 == 0, "free": True} for seed in range(FREE_RUNNING_ROUNDS)]
    return plans


async def test_a_delete_racing_a_group_change_and_a_revoke_ends_removed_with_tombstoned_claims(monkeypatch):
    quiet(monkeypatch)
    failures = []
    for round_number, plan in enumerate(delete_plans()):
        failures += await run_delete_round(monkeypatch, round_number, plan)
    assert failures == [], "\n".join(failures)


async def run_claims_round(monkeypatch, round_number: int, steps: list[str] | None) -> list[str]:
    rng = random.Random(9000 + round_number)
    operator = UserOperation(OperatorType.API)
    isolate_node_manager(monkeypatch, node_manager, {})
    world = await seed_world()
    schedule = Schedule(steps, rng)
    monkeypatch.setattr(node_sync, "_current_state", ReaderBehindTheWritingStep(schedule))
    dispatch = DelayedDispatch(monkeypatch, rng)
    target_initial = world.initial[world.target]
    old = stored_credentials(target_initial)
    async with production_node(monkeypatch, 8100 + round_number) as remote:
        await attach_committed_state(remote, [world.target, world.peer])
        revoke, save, foreign = await asyncio.gather(
            staged(schedule, "revoke", *revoke_of(operator, world.target)),
            staged(schedule, "save", *form_save_of(operator, world.target, form_from(target_initial))),
            staged(
                schedule,
                "foreign",
                *form_save_of(operator, world.peer, form_from(world.initial[world.peer], taken_from(target_initial))),
            ),
        )
        await dispatch.settled()
        await drained(remote.node)
        label = f"round {round_number} (steps {steps})"
        problems = []
        if not revoke.ok:
            problems.append(f"{label}: the revoke ended {revoke!r}")
        if not foreign.refused_with(409):
            problems.append(f"{label}: another user submitting the held or rotated credential ended {foreign!r}")
        if not save.ok and not save.refused_with(409):
            problems.append(f"{label}: the stale form save ended {save!r}")
        if steps is not None:
            save_before_revoke = steps.index("save:write") < steps.index("revoke:write")
            if save.ok != save_before_revoke:
                problems.append(
                    f"{label}: the stale form save submitting the old credentials ended {save!r} although it was "
                    f"written {'before' if save_before_revoke else 'after'} the revoke"
                )
        held = await holders_in_database([world.target, world.peer], old)
        if held:
            problems.append(f"{label}: the rotated-away credentials are stored again: {held}")
        rows = await claim_rows(old)
        for credential in sorted(old):
            holder, retired_at = rows.get(credential, (None, None))
            if credential not in rows or holder != world.target or retired_at is None:
                problems.append(
                    f"{label}: {credential[0]}.{credential[1]} claim is {rows.get(credential)!r}, not retired by "
                    f"user {world.target}"
                )
        problems += [f"{label}: {problem}" for problem in rotated_away_live(remote, old)]
        problems += [f"{label}: {problem}" for problem in await converged(remote, [world.target, world.peer])]
        problems += [f"{label}: {problem}" for problem in shared_live_credentials(remote)]
        problems += [f"{label}: {problem}" for problem in empty_credential_pushes(remote)]
        return problems


def claims_plans() -> list[list[str] | None]:
    plans: list[list[str] | None] = []
    orders = [
        ("revoke", "save", "foreign"),
        ("revoke", "foreign", "save"),
        ("save", "revoke", "foreign"),
        ("save", "foreign", "revoke"),
        ("foreign", "revoke", "save"),
        ("foreign", "save", "revoke"),
    ]
    for order in orders:
        plans.append([f"{name}:load" for name in ("revoke", "save", "foreign")] + [f"{name}:write" for name in order])
        plans.append([step for name in order for step in (f"{name}:load", f"{name}:write")])
    if SERVER_DATABASE:
        plans += [None] * FREE_RUNNING_ROUNDS
    return plans


async def test_a_revoke_racing_stale_and_foreign_submissions_of_the_old_credential_never_hands_it_out(monkeypatch):
    quiet(monkeypatch)
    failures = []
    for round_number, steps in enumerate(claims_plans()):
        failures += await run_claims_round(monkeypatch, round_number, steps)
    assert failures == [], "\n".join(failures)
