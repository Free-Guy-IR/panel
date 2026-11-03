import pytest
from fastapi import status
from sqlalchemy import select
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import OperationalError

from app.db.models import Admin
from app.fork.operation import row_lock_retry
from app.fork.proxy_secrets import locks
from app.operation import admin as admin_operation
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, create_admin

MYSQL_DIALECT = mysql.dialect()


def _refuse_first_removals(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[int]:
    calls: list[int] = []
    real_remove = admin_operation.remove_admin

    async def conflicting_remove(db, db_admin):
        calls.append(len(calls) + 1)
        if len(calls) <= failures:
            exc = OperationalError("SELECT 1 FOR UPDATE NOWAIT", {}, Exception(locks.MYSQL_LOCK_NOWAIT, "nowait"))
            locks.mark_nowait_refusal(exc)
            raise exc
        return await real_remove(db, db_admin)

    monkeypatch.setattr(admin_operation, "remove_admin", conflicting_remove)
    monkeypatch.setattr(
        row_lock_retry, "is_row_lock_conflict", lambda exc, _dialect: locks.is_row_lock_conflict(exc, MYSQL_DIALECT)
    )
    monkeypatch.setattr(row_lock_retry.random, "uniform", lambda low, high: 0)
    return calls


async def _admin_exists(username: str) -> bool:
    async with TestSession() as session:
        return (await session.execute(select(Admin.id).where(Admin.username == username))).scalar() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/api/admin/{username}", "/api/admin/by-username/{username}"])
async def test_removal_by_username_retries_a_refused_late_user_lock(monkeypatch, access_token, route):
    username = create_admin(access_token)["username"]
    calls = _refuse_first_removals(monkeypatch, failures=1)

    response = client.delete(route.format(username=username), headers=auth_headers(access_token))

    assert response.status_code == status.HTTP_204_NO_CONTENT, response.text
    assert calls == [1, 2]
    assert not await _admin_exists(username)


@pytest.mark.asyncio
async def test_removal_by_username_answers_503_and_keeps_the_admin_when_retries_run_out(monkeypatch, access_token):
    username = create_admin(access_token)["username"]
    calls = _refuse_first_removals(monkeypatch, failures=row_lock_retry.ROW_LOCK_ATTEMPTS)

    response = client.delete(f"/api/admin/{username}", headers=auth_headers(access_token))

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, response.text
    assert response.json()["detail"] == row_lock_retry.ADMIN_REMOVAL_LOCK_UNAVAILABLE_DETAIL
    assert calls == [1, 2, 3]
    assert await _admin_exists(username)
