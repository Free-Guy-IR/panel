import pytest
from fastapi import HTTPException

from app.operation import OperatorType
from app.operation.subscription import SubscriptionOperation


def _operation() -> SubscriptionOperation:
    operation = SubscriptionOperation.__new__(SubscriptionOperation)
    operation.operator_type = OperatorType.API
    return operation


@pytest.fixture(autouse=True)
def _clear_ping_state():
    SubscriptionOperation._ping_last_seen.clear()
    yield
    SubscriptionOperation._ping_last_seen.clear()


def test_ping_targets_are_capped():
    links = [f"vless://uuid@host{i}.example.com:443?security=none#r" for i in range(100)]
    targets = SubscriptionOperation._parse_ping_targets(links)
    assert len(targets) == SubscriptionOperation._PING_MAX_HOSTS


def test_ping_targets_skip_unpingable_schemes():
    links = [
        "wireguard://priv@wg.example.com:51820/#r",
        "tg://proxy?server=mt.example.com&port=443&secret=dd#r",
        "vless://uuid@ok.example.com:443?security=none#r",
    ]
    targets = SubscriptionOperation._parse_ping_targets(links)
    assert list(targets) == ["ok.example.com"]


def test_ping_targets_parse_ipv6_brackets():
    targets = SubscriptionOperation._parse_ping_targets(["vless://uuid@[2001:db8::1]:443?security=none#r"])
    assert targets == {"2001:db8::1": (443, False)}


async def test_ping_rate_limit_blocks_rapid_repeat():
    operation = _operation()
    await operation._enforce_ping_rate_limit(1)
    with pytest.raises(HTTPException) as exc_info:
        await operation._enforce_ping_rate_limit(1)
    assert exc_info.value.status_code == 429


async def test_ping_rate_limit_is_per_user():
    operation = _operation()
    await operation._enforce_ping_rate_limit(1)
    await operation._enforce_ping_rate_limit(2)


async def test_ping_rate_limit_allows_after_interval(monkeypatch):
    operation = _operation()
    await operation._enforce_ping_rate_limit(1)
    monkeypatch.setattr(
        SubscriptionOperation,
        "_ping_last_seen",
        {1: SubscriptionOperation._ping_last_seen[1] - SubscriptionOperation._PING_MIN_INTERVAL_SECONDS},
    )
    await operation._enforce_ping_rate_limit(1)
