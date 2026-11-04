from __future__ import annotations

import pytest
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression

from app.db.models import User
from app.fork.crud import admin_scope
from app.fork.crud.user_locks import USER_LOCK_BATCH_SIZE, lock_user_rows


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)


def _id_criteria(statement) -> list[BinaryExpression]:
    return [
        element
        for element in visitors.iterate(statement.whereclause)
        if isinstance(element, BinaryExpression)
        and getattr(element.left, "table", None) is User.__table__
        and element.left.key == "id"
    ]


def _point_id(statement) -> int:
    criteria = _id_criteria(statement)
    assert len(criteria) == 1, str(statement)
    assert criteria[0].operator is operators.eq, str(statement)
    return criteria[0].right.value


@pytest.mark.asyncio
@pytest.mark.parametrize("nowait", [False, True])
async def test_every_user_row_lock_is_a_primary_key_point_lookup_in_ascending_order(nowait):
    session = RecordingSession()
    await lock_user_rows(session, [9, 3, None, 5, 3], nowait=nowait)

    assert [_point_id(statement) for statement in session.statements] == [3, 5, 9]
    assert all(statement._for_update_arg is not None for statement in session.statements)
    assert all(statement._for_update_arg.nowait is nowait for statement in session.statements)


@pytest.mark.asyncio
async def test_a_large_lock_set_never_becomes_one_statement_over_many_rows():
    session = RecordingSession()
    user_ids = list(range(1, USER_LOCK_BATCH_SIZE + 3))
    await lock_user_rows(session, reversed(user_ids))

    assert [_point_id(statement) for statement in session.statements] == user_ids


@pytest.mark.asyncio
async def test_detaching_a_removed_admins_users_touches_each_user_by_its_primary_key_alone(monkeypatch):
    async def committed(db, statement):
        return [(7,), (2,)]

    monkeypatch.setattr(admin_scope, "read_committed_rows", committed)
    session = RecordingSession()
    await admin_scope._detach_committed_owners(session, [11], nowait=True)

    locks = [statement for statement in session.statements if getattr(statement, "_for_update_arg", None) is not None]
    writes = [statement for statement in session.statements if statement not in locks]
    assert [_point_id(statement) for statement in locks] == [2, 7]
    assert [_point_id(statement) for statement in writes] == [2, 7]
    assert all(statement.is_update for statement in writes)
