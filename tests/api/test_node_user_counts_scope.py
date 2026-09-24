from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.db.models import Node, NodeUserUsage
from tests.api import TestSession, client
from tests.api.helpers import auth_headers, unique_name
from tests.api.test_node_user_stats_scope import build_world


@pytest.fixture
def world(access_token, monkeypatch):
    yield from build_world(access_token, monkeypatch)


async def test_node_user_counts_only_count_users_the_caller_can_see(world, access_token):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    async with TestSession() as session:
        node = Node(
            name=unique_name("stats_node"),
            address="127.0.0.1",
            port=1,
            api_port=2,
            server_ca="ca",
            api_key="00000000-0000-0000-0000-000000000000",
            core_config_id=None,
        )
        session.add(node)
        await session.flush()
        session.add_all(
            [
                NodeUserUsage(created_at=now, user_id=world.visible["id"], node_id=node.id, used_traffic=10),
                NodeUserUsage(created_at=now, user_id=world.hidden["id"], node_id=node.id, used_traffic=10),
            ]
        )
        await session.commit()
        node_id = node.id
    params = {
        "start": (now - timedelta(hours=1)).isoformat(),
        "end": (now + timedelta(hours=1)).isoformat(),
        "period": "hour",
        "node_id": node_id,
    }

    def count(token: str) -> int:
        response = client.get("/api/node/user_counts/online", headers=auth_headers(token), params=params)
        assert response.status_code == 200, response.text
        return response.json()["count_during_period"]

    try:
        assert count(access_token) == 2
        assert count(world.group_token) == 1
        assert count(world.own_token) == 0
    finally:
        async with TestSession() as session:
            await session.execute(delete(NodeUserUsage).where(NodeUserUsage.node_id == node_id))
            await session.execute(delete(Node).where(Node.id == node_id))
            await session.commit()
