from app.core.xray import XRayConfig
from app.models.subscription import XDriveTransportConfig
from app.subscription.xray import XrayConfiguration

XDRIVE_SETTINGS = {
    "remoteFolder": "pg-test",
    "service": "gdrive",
    "secrets": ["test-secret"],
    "segmentBytes": 65536,
    "pollIntervalMs": 250,
}


def _config() -> dict:
    return {
        "inbounds": [
            {
                "tag": "xdrive-in",
                "protocol": "vless",
                "port": 18443,
                "settings": {"clients": [], "decryption": "none"},
                "streamSettings": {"network": "xdrive", "xdriveSettings": XDRIVE_SETTINGS},
            }
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
    }


def test_xdrive_inbound_settings_are_read_from_the_core_config():
    settings = XRayConfig(_config()).inbounds_by_tag["xdrive-in"]

    assert settings["network"] == "xdrive"
    assert settings["xdrive_remote_folder"] == "pg-test"
    assert settings["xdrive_service"] == "gdrive"
    assert settings["xdrive_secrets"] == ["test-secret"]
    assert settings["xdrive_segment_bytes"] == 65536
    assert settings["xdrive_poll_interval_ms"] == 250
    assert settings["xdrive_template"] is None


def test_xdrive_transport_is_emitted_with_xray_keys():
    config = XDriveTransportConfig(
        remote_folder="pg-test", service="gdrive", secrets=["test-secret"], segment_bytes=65536, poll_interval_ms=250
    )

    assert XrayConfiguration._transport_xdrive(config, "") == XDRIVE_SETTINGS
