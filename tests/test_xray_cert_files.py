import pytest

from app.core.xray import XRayConfig

CERT = """-----BEGIN CERTIFICATE-----
MIIBczCCARmgAwIBAgIUVQKhFxKK0mr0+1QS3LrGDVoalK8wCgYIKoZIzj0EAwIw
DzENMAsGA1UEAwwEdGVzdDAeFw0yNjA5MTAwMzUwMDhaFw0zNjA5MDcwMzUwMDha
MA8xDTALBgNVBAMMBHRlc3QwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAATxcbWY
KDBgpzLFh9bSpUmFczsT2DzZkAeW3x96HXBhuyOw+5rGfhG46mCb5uDnBwtCBXA3
exSJSMIn8uMd8WQyo1MwUTAdBgNVHQ4EFgQUgUzvmjw781SOP+MSKiih74gBCtQw
HwYDVR0jBBgwFoAUgUzvmjw781SOP+MSKiih74gBCtQwDwYDVR0TAQH/BAUwAwEB
/zAKBggqhkjOPQQDAgNIADBFAiAuKDmRHv6Jr8mJO65oGfp9QHjddIMuUMwC7ONO
bZw1hwIhAJ/yA/ovwkpkK9Uq19rHXbRcqx4Ka2aHGk5Yguh5LIR0
-----END CERTIFICATE-----
"""


def _config(cert_path, key_path) -> dict:
    return {
        "inbounds": [
            {
                "tag": "vless-tls",
                "protocol": "vless",
                "port": 443,
                "settings": {"clients": []},
                "streamSettings": {
                    "network": "tcp",
                    "security": "tls",
                    "tlsSettings": {
                        "certificates": [{"certificateFile": str(cert_path), "keyFile": str(key_path)}]
                    },
                },
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
    }


def test_certificate_files_outside_the_allowed_dirs_are_rejected(tmp_path, monkeypatch):
    import app.core.xray as xray_module

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(xray_module.security_settings, "certificate_dirs", [allowed])

    outside = tmp_path / "secret.pem"
    outside.write_text(CERT)

    with pytest.raises(ValueError, match="outside the allowed"):
        XRayConfig(_config(outside, outside))


def test_traversal_out_of_the_allowed_dir_is_rejected(tmp_path, monkeypatch):
    import app.core.xray as xray_module

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(xray_module.security_settings, "certificate_dirs", [allowed])

    outside = tmp_path / "secret.pem"
    outside.write_text(CERT)

    with pytest.raises(ValueError, match="outside the allowed"):
        XRayConfig(_config(allowed / ".." / "secret.pem", allowed / ".." / "secret.pem"))


def test_certificate_files_inside_the_allowed_dirs_are_inlined(tmp_path, monkeypatch):
    import app.core.xray as xray_module

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    cert = allowed / "cert.pem"
    cert.write_text(CERT)
    key = allowed / "key.pem"
    key.write_text("key-lines")
    monkeypatch.setattr(xray_module.security_settings, "certificate_dirs", [allowed])

    config = XRayConfig(_config(cert, key))

    cert_entry = config["inbounds"][0]["streamSettings"]["tlsSettings"]["certificates"][0]
    assert "certificateFile" not in cert_entry
    assert "keyFile" not in cert_entry
    assert cert_entry["key"] == ["key-lines"]
    assert cert_entry["certificate"]
