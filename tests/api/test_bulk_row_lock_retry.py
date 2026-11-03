import pytest
from fastapi import status
from sqlalchemy import update
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import OperationalError

from app.db.models import User
from app.fork.proxy_secrets import locks
from app.operation import user as user_operation
from tests.api import client
from tests.api.helpers import (
    create_core,
    create_group,
    create_user,
    delete_core,
    delete_group,
    delete_user,
    unique_name,
)

MYSQL_DIALECT = mysql.dialect()


def _driver_error(code: int, message: str) -> OperationalError:
    return OperationalError("SELECT 1 FOR UPDATE NOWAIT", {}, Exception(code, message))


def _force_conflicts(monkeypatch: pytest.MonkeyPatch, failures: int, code: int = locks.MYSQL_LOCK_NOWAIT) -> list[int]:
    calls: list[int] = []
    real_update = user_operation.update_users_datalimit

    async def conflicting_update(db, bulk_model, allowed_group_ids=None, **constraints):
        calls.append(len(calls) + 1)
        if len(calls) <= failures:
            await db.execute(update(User).where(User.id.in_(bulk_model.users)).values(data_limit=User.data_limit + 999))
            raise _driver_error(code, "lock not available")
        return await real_update(db, bulk_model, allowed_group_ids, **constraints)

    monkeypatch.setattr(user_operation, "update_users_datalimit", conflicting_update)
    monkeypatch.setattr(
        user_operation, "is_row_lock_conflict", lambda exc, dialect: locks.is_row_lock_conflict(exc, MYSQL_DIALECT)
    )
    monkeypatch.setattr(user_operation.random, "uniform", lambda low, high: 0)
    return calls


@pytest.fixture
def two_users(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("lock_retry_group"))
    users = [
        create_user(
            access_token, group_ids=[group["id"]], payload={"username": unique_name("lockr"), "data_limit": 100}
        ),
        create_user(
            access_token, group_ids=[group["id"]], payload={"username": unique_name("lockr"), "data_limit": 200}
        ),
    ]
    yield users
    for user in users:
        delete_user(access_token, user["username"])
    delete_group(access_token, group["id"])
    delete_core(access_token, core["id"])


def _post_bulk(access_token: str, user_ids: list[int]):
    return client.post(
        "/api/users/bulk/data_limit",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"amount": 50, "users": user_ids},
    )


def _limits(access_token: str, user_ids: list[int]) -> list[int]:
    listed = {
        u["id"]: u
        for u in client.get("/api/users", headers={"Authorization": f"Bearer {access_token}"}).json()["users"]
    }
    return [listed[user_id]["data_limit"] for user_id in user_ids]


def test_a_retry_after_a_lock_conflict_stores_the_bulk_exactly_once(access_token, two_users, monkeypatch):
    user_ids = [user["id"] for user in two_users]
    calls = _force_conflicts(monkeypatch, failures=1)

    response = _post_bulk(access_token, user_ids)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert calls == [1, 2]
    assert _limits(access_token, user_ids) == [150, 250]


def test_a_retry_after_a_deadlock_stores_the_bulk_exactly_once(access_token, two_users, monkeypatch):
    user_ids = [user["id"] for user in two_users]
    calls = _force_conflicts(monkeypatch, failures=2, code=locks.MYSQL_DEADLOCK)

    response = _post_bulk(access_token, user_ids)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert calls == [1, 2, 3]
    assert _limits(access_token, user_ids) == [150, 250]


def test_when_every_retry_hits_a_lock_conflict_nothing_is_stored_and_the_status_is_retryable(
    access_token, two_users, monkeypatch
):
    user_ids = [user["id"] for user in two_users]
    calls = _force_conflicts(monkeypatch, failures=10)

    response = _post_bulk(access_token, user_ids)

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert response.json()["detail"] == locks.BULK_LOCK_UNAVAILABLE_DETAIL
    assert calls == [1, 2, 3]
    assert _limits(access_token, user_ids) == [100, 200]


def test_an_error_that_is_not_a_lock_conflict_is_not_retried(access_token, two_users, monkeypatch):
    user_ids = [user["id"] for user in two_users]
    calls = _force_conflicts(monkeypatch, failures=10, code=1062)

    try:
        status_code = _post_bulk(access_token, user_ids).status_code
    except OperationalError:
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

    assert status_code != status.HTTP_200_OK
    assert calls == [1]
    assert _limits(access_token, user_ids) == [100, 200]


def test_sqlite_lock_errors_are_never_classified_as_row_lock_conflicts():
    for code in (locks.MYSQL_LOCK_NOWAIT, locks.MYSQL_DEADLOCK, locks.MYSQL_LOCK_WAIT_TIMEOUT):
        assert not locks.is_row_lock_conflict(_driver_error(code, "database is locked"), sqlite.dialect())


def test_the_classifier_matches_nowait_and_deadlock_codes_only():
    mariadb = mysql.dialect()
    mariadb.is_mariadb = True
    assert locks.is_row_lock_conflict(_driver_error(locks.MYSQL_LOCK_NOWAIT, "nowait"), MYSQL_DIALECT)
    assert locks.is_row_lock_conflict(_driver_error(locks.MYSQL_DEADLOCK, "deadlock"), MYSQL_DIALECT)
    assert not locks.is_row_lock_conflict(_driver_error(locks.MYSQL_LOCK_WAIT_TIMEOUT, "timeout"), MYSQL_DIALECT)
    assert locks.is_row_lock_conflict(_driver_error(locks.MYSQL_LOCK_WAIT_TIMEOUT, "nowait"), mariadb)
    assert not locks.is_row_lock_conflict(_driver_error(1062, "duplicate"), MYSQL_DIALECT)
