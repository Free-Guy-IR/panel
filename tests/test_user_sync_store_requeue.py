import asyncio

import pytest
from PasarGuardNodeBridge.common.service_pb2 import User
from PasarGuardNodeBridge.storage import InMemoryUserSyncStore

from app.nats.kv_cas import MemoryCasKv
from app.node.nats_memory import NatsUserSyncStore


def _user(inbounds: list[str]) -> User:
    return User(email="42", inbounds=inbounds)


def _stores():
    return {"in_memory": InMemoryUserSyncStore(), "nats_kv": NatsUserSyncStore(MemoryCasKv())}


@pytest.mark.parametrize("store_name", ["in_memory", "nats_kv"])
@pytest.mark.parametrize("path", ["failed_delivery_requeue", "expired_claim_requeue"])
async def test_requeue_of_an_older_claim_keeps_the_newer_pending_update(store_name, path):
    store = _stores()[store_name]
    lease = 0 if path == "expired_claim_requeue" else 30
    await store.enqueue_users("7", [_user(["vless-in"])])
    claimed = await store.claim_users("7", "worker-a", limit=10, lease_seconds=lease)
    assert [list(item.user.inbounds) for item in claimed] == [["vless-in"]]

    await store.enqueue_users("7", [_user([])])

    if path == "failed_delivery_requeue":
        await store.requeue_users("7", claimed)
    else:
        await asyncio.sleep(0.01)

    delivered = await store.claim_users("7", "worker-b", limit=10, lease_seconds=30)
    assert [list(item.user.inbounds) for item in delivered] == [[]], (
        f"{store_name}/{path}: the user was disabled (empty inbounds) after worker-a claimed the older payload, "
        f"but the next delivery is {[list(item.user.inbounds) for item in delivered]}"
    )
