import pytest

from app.core.wireguard import WireGuardConfig
from app.utils.crypto import generate_wireguard_keypair

PRIVATE_KEY, _ = generate_wireguard_keypair()


def _config(**extra):
    return {
        "interface_name": "wg0",
        "private_key": PRIVATE_KEY,
        "listen_port": 51820,
        "address": ["10.8.0.1/24"],
        **extra,
    }


def test_a_core_without_mtu_sends_no_mtu_key_to_the_node():
    config = WireGuardConfig(_config())
    assert "mtu" not in config
    assert "mtu" not in config.to_json()["config"]


@pytest.mark.parametrize("mtu", [576, 1280, 1420, 9000])
def test_an_mtu_inside_the_node_range_is_kept_as_is(mtu):
    config = WireGuardConfig(_config(mtu=mtu))
    assert config["mtu"] == mtu
    assert config.to_json()["config"]["mtu"] == mtu


@pytest.mark.parametrize("mtu", [575, 9001, 0, -1420, True, False, "1420", 1420.0, None, [1420]])
def test_an_mtu_the_node_would_refuse_is_rejected_before_it_is_saved(mtu):
    with pytest.raises(ValueError, match="mtu must be an integer between 576 and 9000"):
        WireGuardConfig(_config(mtu=mtu))
