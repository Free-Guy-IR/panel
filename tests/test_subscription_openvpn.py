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


def test_render_does_not_use_inline_auth_user_pass():
    # <auth-user-pass>...</auth-user-pass> is not a real OpenVPN directive -
    # real openvpn (2.5.11) rejects it with "option 'auth-user-pass' is not
    # expected to be inline". Only ca/cert/key/tls-crypt support inline PEM
    # blocks.
    conf = OpenVPNConfiguration()
    conf.components.append(_component())
    rendered = conf.render().decode()

    assert "<auth-user-pass>" not in rendered
    assert "</auth-user-pass>" not in rendered
    assert "\nauth-user-pass\n" in rendered or rendered.strip().endswith("auth-user-pass")


def test_render_includes_credentials_as_comments():
    conf = OpenVPNConfiguration()
    conf.components.append(_component(username="7", password="pw"))
    rendered = conf.render().decode()

    assert "# username: 7" in rendered
    assert "# password: pw" in rendered


def test_render_includes_connection_block_per_matching_instance():
    conf = OpenVPNConfiguration()
    conf.components.append(_component(port=1194, protocol="udp"))
    conf.components.append(_component(port=443, protocol="tcp"))
    rendered = conf.render().decode()

    assert "remote 203.0.113.10 1194 udp" in rendered
    assert "remote 203.0.113.10 443 tcp" in rendered
    assert rendered.count("<connection>") == 2
