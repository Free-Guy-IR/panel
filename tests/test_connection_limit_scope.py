from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.fork.routers import connection_limit as routes
from app.models.admin import AdminDetails, AdminRoleData
from app.models.connection_limit import UserConnectionLimitPayload
from app.models.settings import ConnectionLimit


def _admin(*, owner=False, admin_id=7) -> AdminDetails:
    return AdminDetails(
        id=admin_id,
        username="reseller",
        role=AdminRoleData(
            id=3,
            name="reseller",
            is_owner=owner,
            permissions={
                "users": {"read": {"scope": 1}, "update": {"scope": 1}},
                "settings": {"read": True, "update": True},
            },
        ),
    )


def _where_sql(stmt) -> str:
    clause = getattr(stmt, "whereclause", None)
    if clause is None:
        return ""
    return str(clause.compile(compile_kwargs={"literal_binds": True})).lower()


class _Result:
    def __init__(self, *, scalar=None, rows=None):
        self._scalar = scalar
        self._rows = rows or []

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows


class _DB:
    def __init__(self, *, scalar=None, rows=None):
        self.stmts = []
        self._scalar = scalar
        self._rows = rows or []

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return _Result(scalar=self._scalar, rows=self._rows)

    async def scalar(self, stmt):
        self.stmts.append(stmt)
        return 0


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.setattr(routes, "_settings", AsyncMock(return_value=ConnectionLimit()))


@pytest.mark.asyncio
async def test_states_by_user_are_limited_to_the_admins_own_users(settings):
    db = _DB()
    await routes.connection_states_for_users(user_ids=[11, 22], db=db, admin=_admin())

    sql = _where_sql(db.stmts[0])
    assert "admin_id" in sql
    assert "7" in sql


@pytest.mark.asyncio
async def test_owner_states_by_user_are_not_filtered_by_admin(settings):
    db = _DB()
    await routes.connection_states_for_users(user_ids=[11, 22], db=db, admin=_admin(owner=True))

    assert "admin_id" not in _where_sql(db.stmts[0])


@pytest.mark.asyncio
async def test_list_states_are_limited_to_the_admins_own_users(settings):
    db = _DB()
    await routes.list_connection_states(verdict=None, min_devices=None, limit=50, offset=0, db=db, admin=_admin())

    assert any("admin_id" in _where_sql(stmt) for stmt in db.stmts)


@pytest.mark.asyncio
async def test_set_override_hides_another_admins_user():
    db = _DB()
    with pytest.raises(HTTPException) as exc:
        await routes.set_override(
            user_id=99,
            payload=UserConnectionLimitPayload(ip_limit=2),
            db=db,
            admin=_admin(),
        )

    assert exc.value.status_code == 404
    sql = _where_sql(db.stmts[0])
    assert "admin_id" in sql
    assert "7" in sql


@pytest.mark.asyncio
async def test_owner_override_lookup_is_not_filtered_by_admin():
    db = _DB()
    with pytest.raises(HTTPException) as exc:
        await routes.set_override(
            user_id=99,
            payload=UserConnectionLimitPayload(ip_limit=2),
            db=db,
            admin=_admin(owner=True),
        )

    assert exc.value.status_code == 404
    assert "admin_id" not in _where_sql(db.stmts[0])


@pytest.mark.asyncio
async def test_clear_override_hides_another_admins_user():
    db = _DB()
    with pytest.raises(HTTPException) as exc:
        await routes.clear_override(user_id=99, db=db, admin=_admin())

    assert exc.value.status_code == 404
    assert "admin_id" in _where_sql(db.stmts[0])


@pytest.mark.asyncio
async def test_resolve_addresses_hides_another_admins_user(settings):
    db = _DB()
    with pytest.raises(HTTPException) as exc:
        await routes.resolve_user_addresses(user_id=99, db=db, admin=_admin())

    assert exc.value.status_code == 404
    assert "admin_id" in _where_sql(db.stmts[0])
