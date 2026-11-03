import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import and_, func, insert, or_, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Admin,
    AdminRole,
    AdminStatus,
    Group,
    ProxyInbound,
    User,
    UserStatus,
    inbounds_groups_association,
    users_groups_association,
)
from app.node import user as node_user

PARITY_DB_URL = os.environ.get("PARITY_DB_URL")


def _inbound_aggregate(dialect):
    if dialect == "postgresql":
        return func.string_agg(ProxyInbound.tag.distinct(), ",").label("inbound_tags")
    return func.group_concat(ProxyInbound.tag.distinct()).label("inbound_tags")


def _legacy_statement(inbound_tags, dialect="sqlite"):
    inbound_agg = _inbound_aggregate(dialect)
    return (
        select(User.id, User.proxy_settings, User.hwid_limit, inbound_agg)
        .outerjoin(users_groups_association, User.id == users_groups_association.c.user_id)
        .outerjoin(
            Group,
            and_(users_groups_association.c.groups_id == Group.id, Group.is_disabled.is_(False)),
        )
        .outerjoin(inbounds_groups_association, Group.id == inbounds_groups_association.c.group_id)
        .outerjoin(
            ProxyInbound,
            and_(
                inbounds_groups_association.c.inbound_id == ProxyInbound.id,
                ProxyInbound.tag.in_(inbound_tags) if inbound_tags else True,
            ),
        )
        .outerjoin(Admin, Admin.id == User.admin_id)
        .outerjoin(AdminRole, AdminRole.id == Admin.role_id)
        .where(User.status.in_([UserStatus.active, UserStatus.on_hold]))
        .where(
            or_(
                Admin.id.is_(None),
                Admin.status.not_in([AdminStatus.limited, AdminStatus.disabled]),
                and_(Admin.status == AdminStatus.limited, AdminRole.disconnect_users_when_limited.is_not(True)),
                and_(Admin.status == AdminStatus.disabled, AdminRole.disconnect_users_when_disabled.is_not(True)),
            )
        )
        .group_by(User.id)
    )


async def _legacy_payload(db, inbound_tags, user_ids=None):
    stmt = _legacy_statement(list(dict.fromkeys(inbound_tags or [])), db.bind.dialect.name)
    if user_ids is not None:
        stmt = stmt.where(User.id.in_(list(user_ids)))
    payload = []
    for row in (await db.execute(stmt)).all():
        tags = row.inbound_tags.split(",") if row.inbound_tags else []
        if not tags:
            continue
        payload.append(node_user._serialize_user_for_node(row.id, row.proxy_settings, tags, None, row.hwid_limit))
    return payload


def _canonical(payload):
    return {user.email: (sorted(user.inbounds), user.proxies.SerializeToString(deterministic=True)) for user in payload}


NOW = datetime(2026, 11, 4, tzinfo=UTC)
ROLE_JSON = {"permissions": {}, "limits": {}, "features": {}, "access": {}, "hwid": {}, "created_at": NOW}


def _proxy(index):
    return {"vless": {"id": str(uuid.UUID(int=index + 1))}, "trojan": {"password": f"secret-{index:04d}"}}


async def _seed(db):
    tags = ["vless-a", "vless-b", "trojan-c", "orphan-d", "other-core-e"]
    for index, tag in enumerate(tags, start=1):
        await db.execute(insert(ProxyInbound.__table__).values(id=index, tag=tag))
    inbound_id = {tag: index for index, tag in enumerate(tags, start=1)}
    groups = {
        "enabled-ab": (False, ["vless-a", "vless-b"]),
        "enabled-c": (False, ["trojan-c"]),
        "disabled-all": (True, ["vless-a", "vless-b", "trojan-c"]),
        "empty": (False, []),
        "other-core": (False, ["other-core-e"]),
    }
    group_id = {}
    for index, (name, (disabled, members)) in enumerate(groups.items(), start=1):
        await db.execute(insert(Group.__table__).values(id=index, name=name, is_disabled=disabled))
        group_id[name] = index
        for tag in members:
            await db.execute(insert(inbounds_groups_association).values(inbound_id=inbound_id[tag], group_id=index))
    await db.execute(
        insert(AdminRole.__table__).values(
            id=1,
            name="keep",
            disconnect_users_when_limited=False,
            disconnect_users_when_disabled=False,
            **ROLE_JSON,
        )
    )
    await db.execute(insert(AdminRole.__table__).values(id=2, name="cut", **ROLE_JSON))
    admins = [
        (1, "active-admin", AdminStatus.active, 2),
        (2, "limited-keep", AdminStatus.limited, 1),
        (3, "limited-cut", AdminStatus.limited, 2),
        (4, "disabled-cut", AdminStatus.disabled, 2),
    ]
    for admin_id, name, admin_status, role_id in admins:
        await db.execute(
            insert(Admin.__table__).values(
                id=admin_id,
                username=name,
                hashed_password="x",
                status=admin_status,
                role_id=role_id,
                created_at=NOW,
            )
        )
    memberships = [
        [],
        ["enabled-ab"],
        ["enabled-c"],
        ["enabled-ab", "enabled-c"],
        ["disabled-all"],
        ["disabled-all", "enabled-c"],
        ["empty"],
        ["empty", "enabled-ab"],
        ["other-core"],
        ["other-core", "enabled-ab", "disabled-all"],
    ]
    statuses = [UserStatus.active, UserStatus.on_hold, UserStatus.disabled, UserStatus.expired, UserStatus.limited]
    admin_ids = [None, 1, 2, 3, 4]
    user_id = 0
    for member_groups in memberships:
        for user_status in statuses:
            for admin_id in admin_ids:
                user_id += 1
                await db.execute(
                    insert(User.__table__).values(
                        id=user_id,
                        username=f"u{user_id:04d}",
                        status=user_status,
                        admin_id=admin_id,
                        proxy_settings=_proxy(user_id),
                        hwid_limit=None if user_id % 3 else user_id % 7,
                        created_at=NOW,
                    )
                )
                for name in member_groups:
                    await db.execute(insert(users_groups_association).values(user_id=user_id, groups_id=group_id[name]))
    await db.commit()
    return user_id


def _truncate_all(sync_connection):
    names = [table.name for table in Base.metadata.sorted_tables]
    dialect = sync_connection.dialect.name
    if dialect == "mysql":
        sync_connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for name in names:
            sync_connection.execute(text(f"TRUNCATE TABLE `{name}`"))
        sync_connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    elif dialect == "postgresql":
        joined = ", ".join(f'"{name}"' for name in names)
        sync_connection.execute(text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"))
    else:
        for name in reversed(names):
            sync_connection.execute(text(f'DELETE FROM "{name}"'))


async def _open_engine():
    if PARITY_DB_URL:
        engine = create_async_engine(PARITY_DB_URL)
    else:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        if PARITY_DB_URL:
            await connection.run_sync(_truncate_all)
    return engine


async def _compare():
    engine = await _open_engine()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    results = []
    async with sessions() as db:
        total = await _seed(db)
        scopes = [
            (None, None),
            (["vless-a", "vless-b", "trojan-c", "orphan-d"], None),
            (["vless-b"], None),
            (["orphan-d"], None),
            (["other-core-e"], None),
            (["vless-a", "trojan-c"], set(range(1, total + 1, 4))),
            (None, {2, 7, 31, 52, 77, 103, 128, 200, 249}),
        ]
        for inbound_tags, user_ids in scopes:
            new = await node_user.core_users(db, inbound_tags=inbound_tags, user_ids=user_ids)
            old = await _legacy_payload(db, inbound_tags, user_ids=user_ids)
            results.append((inbound_tags, user_ids, _canonical(new), _canonical(old)))
    await engine.dispose()
    return results


def test_inner_joins_send_the_same_users_and_inbounds_as_the_outer_join_query():
    results = asyncio.run(_compare())
    nonempty = 0
    for inbound_tags, user_ids, new, old in results:
        assert new == old, (inbound_tags, user_ids)
        nonempty += bool(new)
    assert nonempty >= 5
    full = results[0][2]
    assert "1" not in full
    assert all(tags for tags, _ in full.values())
    assert not results[3][2]


@pytest.mark.parametrize(
    ("user_groups", "expected"),
    [
        (["enabled-ab"], ["vless-a", "vless-b"]),
        (["disabled-all", "enabled-c"], ["trojan-c"]),
        (["empty", "enabled-ab"], ["vless-a", "vless-b"]),
        (["empty"], None),
        (["disabled-all"], None),
        ([], None),
    ],
)
def test_membership_shapes_reach_the_node_as_before(user_groups, expected):
    async def run():
        engine = await _open_engine()
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db:
            await _seed(db)
            row_id = 9999
            await db.execute(
                insert(User.__table__).values(
                    id=row_id,
                    username="probe",
                    status=UserStatus.active,
                    admin_id=None,
                    proxy_settings=_proxy(row_id),
                    created_at=NOW,
                )
            )
            names = {name: index for index, name in enumerate(["enabled-ab", "enabled-c", "disabled-all", "empty"], 1)}
            for name in user_groups:
                await db.execute(insert(users_groups_association).values(user_id=row_id, groups_id=names[name]))
            await db.commit()
            new = _canonical(await node_user.core_users(db, user_ids={row_id}))
            old = _canonical(await _legacy_payload(db, None, user_ids={row_id}))
        await engine.dispose()
        return new, old

    new, old = asyncio.run(run())
    assert new == old
    if expected is None:
        assert new == {}
    else:
        assert new[str(9999)][0] == expected


def test_the_legacy_reference_aggregates_like_the_app_on_each_dialect():
    pg = str(_legacy_statement(["vless-a"], "postgresql").compile(dialect=postgresql.dialect()))
    assert "string_agg" in pg
    assert "group_concat" not in pg
    lite = str(_legacy_statement(["vless-a"]))
    assert "group_concat" in lite
    assert "string_agg" not in lite
