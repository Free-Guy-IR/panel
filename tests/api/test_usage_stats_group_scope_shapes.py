import pytest
from sqlalchemy import delete, insert

from app.db.crud import user as user_crud
from app.db.models import Group, NodeUserUsage, users_groups_association
from app.models.stats import Period, UserCountMetric
from tests.api import TestSession
from tests.api.test_usage_stats_query_shapes import TIMEZONES, _ranges, usage_history

__all__ = ["usage_history"]


@pytest.fixture
async def scoped_group(usage_history):
    in_group = usage_history["user_ids"][:2]
    async with TestSession() as session:
        group = Group(name="shape_scope_group", inbounds=[])
        session.add(group)
        await session.flush()
        group_id = group.id
        await session.execute(
            insert(users_groups_association), [{"user_id": uid, "groups_id": group_id} for uid in in_group]
        )
        await session.commit()
    yield group_id, in_group
    async with TestSession() as session:
        await session.execute(delete(users_groups_association).where(users_groups_association.c.groups_id == group_id))
        await session.execute(delete(Group).where(Group.id == group_id))
        await session.commit()


async def _scoped_traffic_total(history: dict, user_ids: list[int], start, end) -> int:
    async with TestSession() as session:
        rows = await session.scalars(
            NodeUserUsage.__table__.select()
            .with_only_columns(NodeUserUsage.used_traffic)
            .where(
                NodeUserUsage.user_id.in_(user_ids),
                NodeUserUsage.created_at >= start,
                NodeUserUsage.created_at < end,
            )
        )
        return sum(rows.all())


@pytest.mark.asyncio
@pytest.mark.parametrize("tz", TIMEZONES, ids=["utc", "plus0330", "minus05"])
@pytest.mark.parametrize("period", [Period.hour, Period.day])
async def test_group_scoped_usage_chart_is_the_same_without_the_users_join(
    usage_history, scoped_group, monkeypatch, tz, period
):
    group_id, in_group = scoped_group
    start, end = _ranges(tz)[0]
    results = []
    for needs_join in (False, True):
        with monkeypatch.context() as patch:
            patch.setattr(user_crud, "_usage_history_needs_users_join", lambda *args, needs_join=needs_join: needs_join)
            async with TestSession() as session:
                results.append(
                    await user_crud.get_all_users_usages(
                        session, None, start, end, period, allowed_group_ids=[group_id]
                    )
                )
    assert results[0].model_dump() == results[1].model_dump()
    scoped_total = sum(stat.total_traffic for stats in results[0].stats.values() for stat in stats)
    assert scoped_total == await _scoped_traffic_total(usage_history, in_group, start, end)
    assert scoped_total > 0

    async with TestSession() as session:
        unscoped = await user_crud.get_all_users_usages(session, None, start, end, period)
    assert sum(stat.total_traffic for stats in unscoped.stats.values() for stat in stats) > scoped_total


@pytest.mark.asyncio
@pytest.mark.parametrize("tz", TIMEZONES, ids=["utc", "plus0330", "minus05"])
@pytest.mark.parametrize("metric", [UserCountMetric.online, UserCountMetric.expired, UserCountMetric.limited])
async def test_group_scoped_user_counts_are_the_same_for_both_query_shapes(
    usage_history, scoped_group, monkeypatch, tz, metric
):
    group_id, in_group = scoped_group
    start, end = _ranges(tz)[0]
    results = []
    for use_pairs in (False, True):
        with monkeypatch.context() as patch:
            patch.setattr(
                user_crud, "_user_count_buckets_from_distinct_pairs", lambda dialect, use_pairs=use_pairs: use_pairs
            )
            async with TestSession() as session:
                results.append(
                    await user_crud.get_user_count_metric_stats(
                        session, None, start, end, Period.day, metric, allowed_group_ids=[group_id]
                    )
                )
    assert results[0].model_dump() == results[1].model_dump()
    assert all(stat.count <= len(in_group) for stats in results[0].stats.values() for stat in stats)
    if metric == UserCountMetric.online:
        assert max(stat.count for stats in results[0].stats.values() for stat in stats) == len(in_group)
