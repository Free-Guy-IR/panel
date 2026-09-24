import json
from types import SimpleNamespace

import pytest

from app.core.manager import CoreManager
from app.db.models import CoreType
from app.utils.crypto import generate_wireguard_keypair


def _wg_row(listen_port: int, private_key: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        name="wg",
        type=CoreType.wg,
        config={
            "interface_name": "wg0",
            "private_key": private_key,
            "listen_port": listen_port,
            "address": ["10.0.0.1/24"],
        },
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )


class _Entry:
    def __init__(self, value: bytes):
        self.value = value


class _KV:
    def __init__(self, state: dict):
        self.stored = json.dumps(state).encode()

    async def get(self, key):
        return _Entry(self.stored)

    async def put(self, key, value):
        self.stored = value


@pytest.mark.asyncio
async def test_a_restart_loads_the_cores_the_database_holds_not_an_older_kv_snapshot(monkeypatch):
    private_key, _ = generate_wireguard_keypair()
    before = _wg_row(51820, private_key)
    after = _wg_row(51999, private_key)

    old_worker = CoreManager()
    old_worker._nats_enabled = False
    snapshot_rows = [before]

    async def rows(db, query):
        return list(snapshot_rows), len(snapshot_rows)

    monkeypatch.setattr("app.core.manager.get_core_configs", rows)
    kv_holder = {}

    async def keep_snapshot():
        kv_holder["state"] = await old_worker._snapshot_state()

    monkeypatch.setattr(old_worker, "_persist_state", keep_snapshot)
    await old_worker.initialize(db=None)

    snapshot_rows[:] = [after]

    restarted = CoreManager()
    restarted._nats_enabled = False
    restarted._kv = _KV(kv_holder["state"])
    await restarted.initialize(db=None)

    loaded = (await restarted.get_cores())[1]
    assert loaded.to_json()["config"]["listen_port"] == 51999, (
        "the database says listen_port 51999 (changed while the KV put failed, by a migration, or by a panel "
        f"without NATS), but the restarted worker loaded {loaded.to_json()['config']['listen_port']} from the "
        "persistent JetStream KV snapshot and never read the database"
    )
