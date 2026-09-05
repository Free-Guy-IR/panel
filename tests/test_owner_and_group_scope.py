"""Two things that were reachable through the panel and should not have been.

Handing an admin a user they already own moved no ownership, and yet still
credited their traffic counter a second time. The counter is only ever added
to - never recomputed from the users - so the inflation was permanent, and the
admin limit job compares that very counter against the admin's data limit.

And the bulk group endpoints reached every user in the panel while asking only
whether the caller may use the groups. Which groups a user is in decides which
inbounds they get, so that is the power to cut off or upgrade anyone's users.
"""

import ast
import pathlib

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import bulk_set_owner
from app.db.models import Admin, Base, Group, User, UserStatus
from app.models.proxy import ProxyTable

ROUTER = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers" / "group.py"
BULK_ROUTES = ("bulk_add_groups_to_users", "bulk_remove_users_from_groups")


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _admin(db, username: str, used_traffic: int = 0) -> Admin:
    admin = Admin(username=username, hashed_password="x", role_id=1)
    admin.used_traffic = used_traffic
    db.add(admin)
    await db.flush()
    return admin


async def _user(db, username: str, admin: Admin | None, used_traffic: int) -> User:
    group = Group(name=f"g_{username}", inbounds=[])
    db.add(group)
    await db.flush()
    user = User(username=username, proxy_settings=ProxyTable().dict(), admin_id=admin.id if admin else None)
    user.groups = [group]
    user.status = UserStatus.active
    user.used_traffic = used_traffic
    db.add(user)
    await db.flush()
    return user


async def _traffic(db, admin_id: int) -> int:
    return await db.scalar(select(Admin.used_traffic).where(Admin.id == admin_id))


@pytest.mark.asyncio
async def test_a_user_the_admin_already_owns_moves_no_counter(db):
    owner = await _admin(db, "owner", used_traffic=500)
    user = await _user(db, "already-theirs", owner, used_traffic=500)
    await db.commit()

    await bulk_set_owner(db, [user], owner)

    assert await _traffic(db, owner.id) == 500


@pytest.mark.asyncio
async def test_setting_the_same_owner_twice_does_not_keep_adding(db):
    """The counter is never recomputed, so an inflation here is permanent."""
    old = await _admin(db, "old", used_traffic=300)
    new = await _admin(db, "new", used_traffic=0)
    user = await _user(db, "moving", old, used_traffic=300)
    await db.commit()

    await bulk_set_owner(db, [user], new)
    after_first = await _traffic(db, new.id)
    await bulk_set_owner(db, [user], new)

    assert after_first == 300
    assert await _traffic(db, new.id) == 300
    assert await _traffic(db, old.id) == 0


@pytest.mark.asyncio
async def test_a_real_move_still_shifts_the_traffic_between_both(db):
    old = await _admin(db, "old", used_traffic=1000)
    new = await _admin(db, "new", used_traffic=50)
    user = await _user(db, "moving", old, used_traffic=400)
    await db.commit()

    await bulk_set_owner(db, [user], new)

    assert await _traffic(db, old.id) == 600
    assert await _traffic(db, new.id) == 450


@pytest.mark.asyncio
async def test_a_user_with_no_owner_is_still_credited_to_the_new_one(db):
    """The guard must not swallow this case, which has no old admin to debit."""
    new = await _admin(db, "new", used_traffic=0)
    user = await _user(db, "orphan", None, used_traffic=700)
    await db.commit()

    await bulk_set_owner(db, [user], new)

    assert await _traffic(db, new.id) == 700


def _dependencies(route_name: str) -> set[str]:
    """The permission dependencies declared on one route."""
    tree = ast.parse(ROUTER.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name != route_name:
            continue
        found = set()
        for default in node.args.defaults + node.args.kw_defaults:
            for call in ast.walk(default) if default is not None else ():
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in ("require_permission", "require_scope_all")
                ):
                    args = tuple(ast.literal_eval(a) for a in call.args)
                    found.add(f"{call.func.id}{args}")
        return found
    raise AssertionError(f"route {route_name} not found")


@pytest.mark.parametrize("route", BULK_ROUTES)
def test_the_bulk_group_routes_also_require_the_user_scope(route):
    """They reach every user in the panel, so the group permission is not enough."""
    declared = _dependencies(route)

    assert "require_permission('groups', 'update')" in declared
    assert "require_scope_all('users', 'update')" in declared
