from app.subscription.openvpn import OpenVPNConfiguration


def _component(**overrides):
    base = {
        "remark": "test",
        "address": "203.0.113.10",
        "port": 1194,
        "protocol": "udp",
        "username": "42",
        "password": "secretpass",
        "cipher": "AES-256-GCM",
        "auth": "SHA256",
        "ca_cert": "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----",
        "tls_crypt_key": "-----BEGIN OpenVPN Static key V1-----\nBBBB\n-----END OpenVPN Static key V1-----",
    }
    base.update(overrides)
    return base


def test_render_empty_without_components():
    conf = OpenVPNConfiguration()
    assert conf.render() == b""


def test_render_uses_inline_auth_user_pass():
    # <auth-user-pass>...</auth-user-pass> is rejected by the classic
    # openvpn2 CLI, but openvpn3 (OpenVPN Connect - what real, especially
    # mobile, users actually import this file into) supports it and
    # connects with zero credential prompts - verified with a live
    # `openvpn3 session-start` against this exact renderer's output.
    conf = OpenVPNConfiguration()
    conf.components.append(_component(username="7", password="pw"))
    rendered = conf.render().decode()

    assert "<auth-user-pass>\n7\npw\n</auth-user-pass>" in rendered


def test_render_includes_connection_block_per_matching_instance():
    conf = OpenVPNConfiguration()
    conf.components.append(_component(port=1194, protocol="udp"))
    conf.components.append(_component(port=443, protocol="tcp"))
    rendered = conf.render().decode()

    assert "remote 203.0.113.10 1194 udp" in rendered
    assert "remote 203.0.113.10 443 tcp" in rendered
    assert rendered.count("<connection>") == 2
