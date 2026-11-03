import pytest
from fastapi import status
from sqlalchemy import update
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.exc import OperationalError

from app.db.crud import bulk as bulk_crud
from app.db.models import User
from app.fork.operation import row_lock_retry
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


def _mariadb_dialect():
    dialect = mysql.dialect()
    dialect.is_mariadb = True
    return dialect


def _driver_error(code: int, message: str) -> OperationalError:
    return OperationalError("SELECT 1 FOR UPDATE NOWAIT", {}, Exception(code, message))


def _nowait_refusal(code: int, message: str) -> OperationalError:
    exc = _driver_error(code, message)
    locks.mark_nowait_refusal(exc)
    return exc


def _force_conflicts(
    monkeypatch: pytest.MonkeyPatch,
    failures: int,
    code: int = locks.MYSQL_LOCK_NOWAIT,
    *,
    from_nowait: bool = True,
    dialect=MYSQL_DIALECT,
) -> list[int]:
    calls: list[int] = []
    real_update = user_operation.update_users_datalimit

    async def conflicting_update(db, bulk_model, allowed_group_ids=None, **constraints):
        calls.append(len(calls) + 1)
        if len(calls) <= failures:
            await db.execute(update(User).where(User.id.in_(bulk_model.users)).values(data_limit=User.data_limit + 999))
            raise (_nowait_refusal if from_nowait else _driver_error)(code, "lock not available")
        return await real_update(db, bulk_model, allowed_group_ids, **constraints)

    monkeypatch.setattr(user_operation, "update_users_datalimit", conflicting_update)
    monkeypatch.setattr(
        row_lock_retry, "is_row_lock_conflict", lambda exc, _dialect: locks.is_row_lock_conflict(exc, dialect)
    )
    monkeypatch.setattr(row_lock_retry.random, "uniform", lambda low, high: 0)
    return calls


def _conflict_after_commit(monkeypatch: pytest.MonkeyPatch, code: int) -> list[int]:
    calls: list[int] = []
    real_update = user_operation.update_users_datalimit

    async def update_then_conflict(db, bulk_model, allowed_group_ids=None, **constraints):
        calls.append(len(calls) + 1)
        await real_update(db, bulk_model, allowed_group_ids, **constraints)
        if code == locks.MYSQL_DEADLOCK:
            raise _driver_error(code, "deadlock after the commit")
        raise _nowait_refusal(code, "lock conflict after the commit")

    monkeypatch.setattr(user_operation, "update_users_datalimit", update_then_conflict)
    monkeypatch.setattr(
        row_lock_retry, "is_row_lock_conflict", lambda exc, _dialect: locks.is_row_lock_conflict(exc, MYSQL_DIALECT)
    )
    monkeypatch.setattr(row_lock_retry.random, "uniform", lambda low, high: 0)
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
    mariadb = _mariadb_dialect()
    assert locks.is_row_lock_conflict(_nowait_refusal(locks.MYSQL_LOCK_NOWAIT, "nowait"), MYSQL_DIALECT)
    assert locks.is_row_lock_conflict(_driver_error(locks.MYSQL_DEADLOCK, "deadlock"), MYSQL_DIALECT)
    assert locks.is_row_lock_conflict(_driver_error(locks.MYSQL_DEADLOCK, "deadlock"), mariadb)
    assert not locks.is_row_lock_conflict(_driver_error(locks.MYSQL_LOCK_WAIT_TIMEOUT, "timeout"), MYSQL_DIALECT)
    assert not locks.is_row_lock_conflict(_nowait_refusal(locks.MYSQL_LOCK_WAIT_TIMEOUT, "timeout"), MYSQL_DIALECT)
    assert locks.is_row_lock_conflict(_nowait_refusal(locks.MYSQL_LOCK_WAIT_TIMEOUT, "nowait"), mariadb)
    assert not locks.is_row_lock_conflict(_driver_error(1062, "duplicate"), MYSQL_DIALECT)
    assert not locks.is_row_lock_conflict(_nowait_refusal(1062, "duplicate"), MYSQL_DIALECT)


def test_a_genuine_mariadb_lock_wait_timeout_is_not_a_row_lock_conflict():
    mariadb = _mariadb_dialect()
    assert not locks.is_row_lock_conflict(_driver_error(locks.MYSQL_LOCK_WAIT_TIMEOUT, "timeout"), mariadb)
    assert not locks.is_row_lock_conflict(_driver_error(locks.MYSQL_LOCK_NOWAIT, "nowait"), MYSQL_DIALECT)


def test_a_genuine_mariadb_lock_wait_timeout_passes_through_without_a_retry(access_token, two_users, monkeypatch):
    user_ids = [user["id"] for user in two_users]
    calls = _force_conflicts(
        monkeypatch, failures=10, code=locks.MYSQL_LOCK_WAIT_TIMEOUT, from_nowait=False, dialect=_mariadb_dialect()
    )

    try:
        status_code = _post_bulk(access_token, user_ids).status_code
    except OperationalError:
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

    assert status_code != status.HTTP_200_OK
    assert calls == [1]
    assert _limits(access_token, user_ids) == [100, 200]


def test_a_mariadb_nowait_refusal_is_retried(access_token, two_users, monkeypatch):
    user_ids = [user["id"] for user in two_users]
    calls = _force_conflicts(monkeypatch, failures=1, code=locks.MYSQL_LOCK_WAIT_TIMEOUT, dialect=_mariadb_dialect())

    response = _post_bulk(access_token, user_ids)

    assert response.status_code == status.HTTP_200_OK, response.text
    assert calls == [1, 2]
    assert _limits(access_token, user_ids) == [150, 250]


@pytest.mark.parametrize("code", [locks.MYSQL_LOCK_NOWAIT, locks.MYSQL_DEADLOCK])
def test_a_lock_conflict_after_the_commit_is_never_retried_and_the_amount_is_applied_once(
    access_token, two_users, monkeypatch, code
):
    user_ids = [user["id"] for user in two_users]
    calls = _conflict_after_commit(monkeypatch, code)

    try:
        status_code = _post_bulk(access_token, user_ids).status_code
    except OperationalError:
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR

    assert status_code != status.HTTP_200_OK
    assert calls == [1]
    assert _limits(access_token, user_ids) == [150, 250]


class _RefusingSession:
    def __init__(self, exc: OperationalError):
        self.exc = exc

    async def execute(self, statement):
        raise self.exc


@pytest.mark.asyncio
@pytest.mark.parametrize("nowait", [True, False])
async def test_only_the_nowait_lock_statement_marks_its_refusal(nowait):
    exc = _driver_error(locks.MYSQL_LOCK_WAIT_TIMEOUT, "lock wait")

    with pytest.raises(OperationalError) as raised:
        await bulk_crud._lock_rows_by_id(_RefusingSession(exc), User.id.is_not(None), (User.id,), [1, 2], nowait=nowait)

    assert raised.value is exc
    assert locks.is_row_lock_conflict(exc, _mariadb_dialect()) is nowait
