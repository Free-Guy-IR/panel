from copy import deepcopy

from tests.api.helpers import create_core, delete_core, get_inbound_details, unique_name
from tests.api.sample_data import XRAY_CONFIG


def _reality_settings(fingerprint=None):
    settings = deepcopy(XRAY_CONFIG["inbounds"][1]["streamSettings"]["realitySettings"])
    if fingerprint is not None:
        settings["fingerprint"] = fingerprint
    return settings


def _inbound(tag, port, stream_settings):
    return {
        "tag": tag,
        "listen": "0.0.0.0",
        "port": port,
        "protocol": "vless",
        "settings": {"clients": [], "decryption": "none"},
        "streamSettings": stream_settings,
    }


def test_inbound_details_report_security_and_reality_fingerprint(access_token):
    reality_tag = unique_name("reality_fp")
    default_tag = unique_name("reality_default_fp")
    plain_tag = unique_name("plain_ws")
    config = {
        "inbounds": [
            _inbound(
                reality_tag,
                41443,
                {"network": "raw", "security": "reality", "realitySettings": _reality_settings("android")},
            ),
            _inbound(
                default_tag, 41444, {"network": "raw", "security": "reality", "realitySettings": _reality_settings()}
            ),
            _inbound(plain_tag, 41445, {"network": "ws", "wsSettings": {"path": "/ws"}, "security": "none"}),
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }
    core = create_core(access_token, config=config, fallbacks=[])
    try:
        details = {item["tag"]: item for item in get_inbound_details(access_token)}
        assert details[reality_tag]["security"] == "reality"
        assert details[reality_tag]["fingerprint"] == "android"
        assert details[default_tag]["security"] == "reality"
        assert details[default_tag]["fingerprint"] == "chrome"
        assert details[plain_tag]["security"] == "none"
        assert details[plain_tag]["fingerprint"] is None
    finally:
        delete_core(access_token, core["id"])
