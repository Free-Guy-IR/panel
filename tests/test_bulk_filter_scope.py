from sqlalchemy import select

from app.db.crud.bulk import _create_final_filter
from app.db.models import User
from app.models.user import BulkUserFilter, UserStatus


def _sql(bulk_model):
    return str(select(User.id).where(_create_final_filter(bulk_model)).compile(compile_kwargs={"literal_binds": True}))


def test_no_filters_targets_everyone():
    assert _create_final_filter(BulkUserFilter()) is True


def test_status_applies_to_explicitly_selected_users():
    sql = _sql(BulkUserFilter(users=[1, 2, 3], status=[UserStatus.expired]))
    assert "users.status" in sql
    assert "users.id IN (1, 2, 3)" in sql
    assert " OR " not in sql


def test_status_still_applies_when_only_groups_are_given():
    sql = _sql(BulkUserFilter(group_ids=[7], status=[UserStatus.active]))
    assert "users.status" in sql


def test_explicit_users_and_groups_stay_additive_targets():
    sql = _sql(BulkUserFilter(users=[1], group_ids=[7]))
    assert " OR " in sql
    assert "users.id IN (1)" in sql


def test_admins_and_groups_are_still_combined_with_and():
    sql = _sql(BulkUserFilter(admins=[5], group_ids=[7]))
    assert " OR " not in sql
    assert "users.admin_id IN (5)" in sql


def test_only_users_selected_produces_a_plain_id_filter():
    sql = _sql(BulkUserFilter(users=[9]))
    assert "users.id IN (9)" in sql
    assert " OR " not in sql
