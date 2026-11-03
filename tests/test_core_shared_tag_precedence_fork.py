from types import SimpleNamespace

import pytest

from app.core.manager import CoreManager
from app.db.models import CoreType
from app.utils.crypto import generate_wireguard_keypair


def _xray_row(core_id: int, encryption: str, own_tag: str, port: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=core_id,
        name=f"xray-{core_id}",
        type=CoreType.xray,
        config={
            "inbounds": [
                {
                    "tag": "shared",
                    "port": port,
                    "protocol": "vless",
                    "settings": {"clients": [], "decryption": f"dec-{encryption}", "encryption": encryption},
                    "streamSettings": {"network": "tcp"},
                },
                {
                    "tag": own_tag,
                    "port": port + 1,
                    "protocol": "vless",
                    "settings": {"clients": [], "decryption": "none"},
                    "streamSettings": {"network": "tcp"},
                },
            ],
            "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
        },
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )


def _wg_row(core_id: int) -> SimpleNamespace:
    private_key, _ = generate_wireguard_keypair()
    return SimpleNamespace(
        id=core_id,
        name="wg",
        type=CoreType.wg,
        config={
            "interface_name": "wg0",
            "private_key": private_key,
            "listen_port": 51820,
            "address": ["10.0.0.1/24"],
        },
        exclude_inbound_tags=set(),
        fallbacks_inbound_tags=set(),
    )


async def _boot(monkeypatch, rows) -> CoreManager:
    async def core_configs(db, query):
        return list(rows), len(rows)

    monkeypatch.setattr("app.core.manager.get_core_configs", core_configs)
    manager = CoreManager()
    manager._nats_enabled = False
    await manager.initialize(db=None)
    return manager


async def _shared_encryption(manager: CoreManager) -> str:
    return (await manager.get_inbounds_by_tag())["shared"]["encryption"]


CORE_1 = _xray_row(1, "enc-core-1", "only-1", 1001)
CORE_2 = _xray_row(2, "enc-core-2", "only-2", 2001)


@pytest.mark.asyncio
async def test_a_shared_tag_resolves_to_the_highest_core_id_whatever_order_the_database_returns(monkeypatch):
    ascending = await _boot(monkeypatch, [CORE_1, CORE_2])
    descending = await _boot(monkeypatch, [CORE_2, CORE_1])

    assert await _shared_encryption(ascending) == "enc-core-2"
    assert await _shared_encryption(descending) == "enc-core-2", (
        "two cores created in the same second tie on created_at, so the database may return them in either order; "
        "the shared tag must still take core 2's VLESS encryption"
    )


@pytest.mark.asyncio
async def test_a_shared_tag_keeps_the_same_vless_encryption_after_a_restart(monkeypatch):
    live = await _boot(monkeypatch, [CORE_1, CORE_2])
    await live._handle_core_message({"action": "remove", "core_id": 1})
    await live._handle_core_message(
        {
            "action": "update",
            "core": {
                "id": CORE_1.id,
                "type": CORE_1.type,
                "config": CORE_1.config,
                "exclude_inbound_tags": [],
                "fallbacks_inbound_tags": [],
            },
        }
    )
    before_restart = await _shared_encryption(live)

    restarted = await _boot(monkeypatch, [CORE_1, CORE_2])
    after_restart = await _shared_encryption(restarted)

    assert before_restart == after_restart == "enc-core-2", (
        f"the live worker served {before_restart!r} for the shared tag after a NATS remove and re-add of core 1, "
        f"and the restarted worker serves {after_restart!r}: clients holding the old link stop connecting"
    )


@pytest.mark.asyncio
async def test_multi_core_tags_still_resolve_to_their_own_core(monkeypatch):
    manager = await _boot(monkeypatch, [_wg_row(3), CORE_2, CORE_1])

    inbounds_by_tag = await manager.get_inbounds_by_tag()
    assert inbounds_by_tag["only-1"]["port"] == 1002
    assert inbounds_by_tag["only-2"]["port"] == 2002
    assert inbounds_by_tag["shared"]["port"] == 2001
    assert set(await manager.get_inbounds()) == set(inbounds_by_tag)
    assert {"shared", "only-1", "only-2"} <= set(inbounds_by_tag)
    assert len(inbounds_by_tag) == len(set(inbounds_by_tag))
    assert set(inbounds_by_tag) - {"shared", "only-1", "only-2"}, "the WireGuard core's inbound is missing"

    await manager._remove_core_local(2)
    after_removal = await manager.get_inbounds_by_tag()
    assert after_removal["shared"]["encryption"] == "enc-core-1"
    assert "only-2" not in after_removal
    assert after_removal["only-1"]["port"] == 1002
