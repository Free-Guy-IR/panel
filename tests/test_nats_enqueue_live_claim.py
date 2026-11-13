from PasarGuardNodeBridge.common.service_pb2 import User

from app.node.nats_memory import NatsUserSyncStore
from tests.test_nats_sync_integration import jetstream  # noqa: F401


async def test_an_update_enqueued_under_a_live_claim_stays_with_its_owner_until_it_acks(jetstream):  # noqa: F811
    owner = NatsUserSyncStore(jetstream.kv)
    other = NatsUserSyncStore(jetstream.kv)
    try:
        await owner.enqueue_users("1", [User(email="u", inbounds=["v1"])])
        in_flight = await owner.claim_users("1", "a", 10, 60)
        assert [list(item.user.inbounds) for item in in_flight] == [["v1"]]
        await owner.enqueue_users("1", [User(email="u", inbounds=["v2"])])
        captured, active = await other.capture_queued("1")
        assert (captured, active) == ({}, 1)
        assert await other.claim_users("1", "b", 10, 60) == []
        assert await owner.ack_users("1", [item.token for item in in_flight]) == []
        captured, active = await other.capture_queued("1")
        assert (len(captured), active) == (1, 0)
        assert [list(item.user.inbounds) for item in await other.claim_users("1", "b", 10, 60)] == [["v2"]]
    finally:
        await owner.close()
        await other.close()
