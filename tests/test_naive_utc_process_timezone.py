import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import DateTime, bindparam, delete, extract, select

from app.db.crud.user import get_users_subscription_agent_counts
from app.db.models import Node, NodeUsage, User, UserSubscriptionUpdate
from tests.api import GetTestDB, engine
from tests.api.helpers import unique_name

RETENTION_DAYS = 7


@pytest.fixture
def panel_process_timezone(request):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = request.param
    time.tzset()
    yield request.param
    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


def _as_naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment


@pytest.fixture
async def node_id():
    async with GetTestDB() as db:
        node = Node(
            name=f"naive-utc-{uuid4().hex[:12]}",
            address="127.0.0.1",
            port=62050,
            api_port=62051,
            server_ca="ca",
            api_key=str(uuid4()),
            core_config_id=None,
        )
        db.add(node)
        await db.commit()
        created = node.id
    yield created
    async with GetTestDB() as db:
        await db.execute(delete(NodeUsage).where(NodeUsage.node_id == created))
        await db.execute(delete(Node).where(Node.id == created))
        await db.commit()


@pytest.fixture
async def user_id():
    async with GetTestDB() as db:
        user = User(username=unique_name("naive_utc"))
        db.add(user)
        await db.commit()
        created = user.id
    yield created
    async with GetTestDB() as db:
        await db.execute(delete(UserSubscriptionUpdate).where(UserSubscriptionUpdate.user_id == created))
        await db.execute(delete(User).where(User.id == created))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "panel_process_timezone",
    ["UTC", "Asia/Tehran", "America/New_York", "America/Los_Angeles"],
    indirect=True,
)
async def test_node_usage_retention_deletes_nothing_inside_the_retention_period(
    panel_process_timezone, node_id, monkeypatch
):
    from app.fork.jobs import cleanup_node_usages as job

    monkeypatch.setattr(job, "GetDB", GetTestDB)
    monkeypatch.setattr(job.job_settings, "node_usages_retention_days", RETENTION_DAYS)
    boundary = datetime.now(UTC).replace(microsecond=0) - timedelta(days=RETENTION_DAYS)
    inside_by_an_hour = boundary + timedelta(hours=1)
    inside_by_twenty_minutes = boundary + timedelta(minutes=20)
    outside_by_an_hour = boundary - timedelta(hours=1)

    async with GetTestDB() as db:
        for moment in (inside_by_an_hour, inside_by_twenty_minutes, outside_by_an_hour):
            db.add(NodeUsage(node_id=node_id, created_at=moment, uplink=1, downlink=1))
        await db.commit()

    await job.cleanup_node_usages()

    async with GetTestDB() as db:
        left = sorted(
            _as_naive_utc(moment)
            for moment in (await db.scalars(select(NodeUsage.created_at).where(NodeUsage.node_id == node_id))).all()
        )
    expected = sorted(_as_naive_utc(moment) for moment in (inside_by_an_hour, inside_by_twenty_minutes))
    assert left == expected, (
        f"{engine.dialect.name} under {panel_process_timezone}: retention boundary {_as_naive_utc(boundary):%Y-%m-%d %H:%M} UTC"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("panel_process_timezone", ["UTC", "Asia/Tehran", "America/New_York"], indirect=True)
async def test_this_hours_subscription_update_is_counted_whatever_the_process_timezone(panel_process_timezone, user_id):
    async with GetTestDB() as db:
        db.add(UserSubscriptionUpdate(user_id=user_id, user_agent="naive-utc-client"))
        await db.commit()

    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    end = start + timedelta(hours=3)
    async with GetTestDB() as db:
        counts = await get_users_subscription_agent_counts(db, user_id=user_id, start=start, end=end)

    assert counts == [("naive-utc-client", 1)], (
        f"{engine.dialect.name} under {panel_process_timezone}: window {start:%H:%M}-{end:%H:%M} UTC"
    )


@pytest.mark.skipif(
    engine.dialect.name != "postgresql", reason="asyncpg binds timestamptz parameters on PostgreSQL only"
)
@pytest.mark.asyncio
@pytest.mark.parametrize("panel_process_timezone", ["UTC", "Asia/Tehran", "America/New_York"], indirect=True)
async def test_a_naive_value_bound_to_a_timestamptz_parameter_means_utc(panel_process_timezone):
    aware = datetime(2026, 10, 17, 12, 0, 0, tzinfo=UTC)
    naive = aware.replace(tzinfo=None)
    statement = select(
        extract("epoch", bindparam("naive", type_=DateTime(timezone=True))),
        extract("epoch", bindparam("aware", type_=DateTime(timezone=True))),
    )

    async with engine.connect() as connection:
        epochs = (await connection.execute(statement, {"naive": naive, "aware": aware})).one()

    assert [float(epoch) for epoch in epochs] == [aware.timestamp(), aware.timestamp()], (
        f"{engine.dialect.name} under {panel_process_timezone}"
    )
