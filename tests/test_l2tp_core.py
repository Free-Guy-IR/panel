import json

import pytest

from app.core.l2tp import L2TP_PORT, L2TPConfig, validate_psk
from app.models.protocol import ProxyProtocol
from app.utils.l2tp import (
    L2TP_PASSWORD_LENGTH,
    ensure_l2tp_core_material,
    generate_l2tp_password,
    generate_l2tp_psk,
)


def _cfg(**overrides):
    base = {
        "inbound_tag": "l2tp-main",
        "server_addr": "203.0.113.10",
        "pool": "10.31.0.0/24",
        "psk": "correct-horse-battery",
    }
    base.update(overrides)
    return L2TPConfig(base)


def test_metadata_keeps_psk_inside_finalmask_only():
    cfg = _cfg()
    assert cfg.inbounds == ["l2tp-main"]
    meta = cfg.inbounds_by_tag["l2tp-main"]
    assert meta["protocol"] == "l2tp"
    assert meta["port"] == L2TP_PORT
    assert "psk" not in meta
    assert meta["finalmask"]["l2tp"]["psk"] == "correct-horse-battery"
    assert meta["finalmask"]["l2tp"]["server_addr"] == "203.0.113.10"
    assert cfg.protocols == frozenset({ProxyProtocol.l2tp})
    assert json.loads(cfg.to_str())["psk"] == "correct-horse-battery"


def test_defaults_are_filled_in():
    cfg = _cfg()
    assert cfg["local_ip"] == "10.31.0.1"
    assert cfg["dns"] == ["1.1.1.1", "8.8.8.8"]
    assert cfg["ike_proposals"] == []
    assert cfg["esp_proposals"] == []
    assert cfg["legacy_clients"] is False
    assert cfg["egress_interface"] == ""


def test_hostname_and_ip_server_addr_are_accepted():
    assert _cfg(server_addr="vpn.example.com.")["server_addr"] == "vpn.example.com"
    assert _cfg(server_addr="2001:db8::1")["server_addr"] == "2001:db8::1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"inbound_tag": "../etc"},
        {"inbound_tag": "a b"},
        {"inbound_tag": ""},
        {"server_addr": ""},
        {"server_addr": "not a host"},
        {"pool": "fd00::/64"},
        {"pool": "10.31.0.0/31"},
        {"pool": "nope"},
        {"local_ip": "10.99.0.1"},
        {"dns": ["one.one.one.one"]},
        {"ike_proposals": ["aes256-sha1; rm -rf /"]},
        {"egress_interface": "eth0; id"},
        {"psk": "short"},
        {"psk": 'has"quote-inside'},
        {"psk": "has{brace}inside"},
        {"psk": "has space inside"},
        {"psk": "x" * 129},
    ],
)
def test_rejects_bad_values(overrides):
    with pytest.raises(ValueError):
        _cfg(**overrides)


def test_exclude_and_fallback_tags_are_rejected():
    with pytest.raises(ValueError):
        L2TPConfig(
            {"inbound_tag": "l", "server_addr": "1.2.3.4", "pool": "10.31.0.0/24", "psk": "correct-horse"}, {"x"}
        )
    with pytest.raises(ValueError):
        L2TPConfig(
            {"inbound_tag": "l", "server_addr": "1.2.3.4", "pool": "10.31.0.0/24", "psk": "correct-horse"},
            fallbacks_inbound_tags={"x"},
        )


def test_from_json_revalidates_instead_of_trusting_the_snapshot():
    good = _cfg().to_json()
    restored = L2TPConfig.from_json(good)
    assert restored.inbounds_by_tag["l2tp-main"]["finalmask"]["l2tp"]["psk"] == "correct-horse-battery"

    bad = dict(good)
    bad["config"] = {**good["config"], "psk": "2000"}
    with pytest.raises(ValueError):
        L2TPConfig.from_json(bad)


def test_ensure_material_generates_a_unique_valid_psk():
    first = ensure_l2tp_core_material({"inbound_tag": "l", "pool": "10.31.0.0/24"})["psk"]
    second = ensure_l2tp_core_material({"inbound_tag": "l", "pool": "10.31.0.0/24"})["psk"]
    assert first != second
    assert len(first) >= 24
    assert validate_psk(first) == first
    assert ensure_l2tp_core_material({"psk": "keep-me-please"})["psk"] == "keep-me-please"
    assert ensure_l2tp_core_material({"psk": "   "})["psk"] not in ("", "   ")


def test_generated_secrets_have_the_expected_shape():
    assert validate_psk(generate_l2tp_psk())
    password = generate_l2tp_password()
    assert len(password) == L2TP_PASSWORD_LENGTH
    assert password.isalnum()
    assert generate_l2tp_password() != password


WRONG_TYPES = [0, 1, True, False, [], {}, object(), b"bytes"]


@pytest.mark.parametrize("value", [*WRONG_TYPES, None, ["10.0.0.0/24"]])
def test_pool_of_a_wrong_type_raises_type_error(value):
    with pytest.raises(TypeError):
        _cfg(pool=value)


@pytest.mark.parametrize(
    "value", ["", "   ", "nope", "10.0.0.0/33", "::/0", "2001:db8::/32", "167772160", "10.0.0.0/31"]
)
def test_pool_of_a_wrong_value_raises_value_error(value):
    with pytest.raises(ValueError):
        _cfg(pool=value)


@pytest.mark.parametrize("value", WRONG_TYPES)
def test_local_ip_of_a_wrong_type_raises_type_error(value):
    with pytest.raises(TypeError):
        _cfg(local_ip=value)


@pytest.mark.parametrize("value", ["10.99.0.1", "::1", "2001:db8::1", "not-an-ip"])
def test_local_ip_of_a_wrong_value_raises_value_error(value):
    with pytest.raises(ValueError):
        _cfg(local_ip=value)


@pytest.mark.parametrize("value", [None, "", "   "])
def test_unset_local_ip_falls_back_to_the_first_host(value):
    assert _cfg(local_ip=value)["local_ip"] == "10.31.0.1"


@pytest.mark.parametrize("value", [*WRONG_TYPES, None])
def test_psk_of_a_wrong_type_raises_type_error(value):
    with pytest.raises(TypeError):
        _cfg(psk=value)


@pytest.mark.parametrize("value", [*WRONG_TYPES, None])
def test_inbound_tag_of_a_wrong_type_raises_type_error(value):
    with pytest.raises(TypeError):
        _cfg(inbound_tag=value)


@pytest.mark.parametrize("value", [*WRONG_TYPES, None])
def test_server_addr_of_a_wrong_type_raises_type_error(value):
    with pytest.raises(TypeError):
        _cfg(server_addr=value)


@pytest.mark.parametrize("value", WRONG_TYPES)
def test_egress_interface_of_a_wrong_type_raises_type_error(value):
    with pytest.raises(TypeError):
        _cfg(egress_interface=value)


@pytest.mark.parametrize("value", [1, True, "1.1.1.1", {"a": "1.1.1.1"}, [1], [None]])
def test_dns_of_a_wrong_type_raises_type_error(value):
    with pytest.raises(TypeError):
        _cfg(dns=value)


@pytest.mark.parametrize("value", [["one.one.one.one"], ["::1"], ["1.1.1.1", "2001:db8::1"], [""]])
def test_dns_of_a_wrong_value_raises_value_error(value):
    with pytest.raises(ValueError):
        _cfg(dns=value)


@pytest.mark.parametrize("value", [1, True, "aes256-sha1", {"a": "b"}, [1], [None]])
def test_proposals_of_a_wrong_type_raise_type_error(value):
    with pytest.raises(TypeError):
        _cfg(ike_proposals=value)
    with pytest.raises(TypeError):
        _cfg(esp_proposals=value)


@pytest.mark.parametrize("value", [["aes256-sha1; rm -rf /"], ["AES256 SHA1"], ["a b"]])
def test_proposals_of_a_wrong_value_raise_value_error(value):
    with pytest.raises(ValueError):
        _cfg(ike_proposals=value)
    with pytest.raises(ValueError):
        _cfg(esp_proposals=value)


@pytest.mark.parametrize("value", [0, 1, "yes", [], {}, None])
def test_legacy_clients_must_be_a_boolean(value):
    with pytest.raises(TypeError):
        _cfg(legacy_clients=value)


def test_legacy_clients_accepts_real_booleans():
    assert _cfg(legacy_clients=True)["legacy_clients"] is True
    assert _cfg(legacy_clients=False)["legacy_clients"] is False


def test_every_rejection_is_a_value_or_type_error_never_something_else():
    hostile = [None, 0, 1, True, False, [], {}, object(), b"x", "x", ["x"], {"x": "y"}, 1.5, -1]
    keys = (
        "inbound_tag",
        "server_addr",
        "pool",
        "local_ip",
        "psk",
        "egress_interface",
        "dns",
        "ike_proposals",
        "esp_proposals",
        "legacy_clients",
    )
    for key in keys:
        for value in hostile:
            try:
                _cfg(**{key: value})
            except ValueError, TypeError:
                pass
