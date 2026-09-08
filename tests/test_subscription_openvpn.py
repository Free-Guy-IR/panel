import io
import zipfile

from app.subscription.openvpn import OpenVPNConfiguration


def _unzip(blob: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        return {n: zf.read(n).decode() for n in zf.namelist()}


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
    files = _unzip(conf.render())
    rendered = chr(10).join(files.values())

    assert "<auth-user-pass>\n7\npw\n</auth-user-pass>" in rendered


def test_render_includes_connection_block_per_matching_instance():
    conf = OpenVPNConfiguration()
    conf.components.append(_component(port=1194, protocol="udp"))
    conf.components.append(_component(port=443, protocol="tcp"))
    files = _unzip(conf.render())
    rendered = chr(10).join(files.values())

    assert "remote 203.0.113.10 1194 udp" in rendered
    assert "remote 203.0.113.10 443 tcp" in rendered
    assert rendered.count("<connection>") == 2


def test_one_file_per_protocol_each_carrying_every_remote_of_that_protocol():
    """A direct and a tunnelled remote of the same protocol belong in ONE file.

    This is what lets the subscription page offer exactly two downloads - a TCP
    config and a UDP config - while each still fails over between the direct
    server and the relay.
    """
    conf = OpenVPNConfiguration()
    conf.components.append(_component(address="direct.example.com", port=1194, protocol="udp"))
    conf.components.append(_component(address="relay.example.com", port=3000, protocol="udp"))
    conf.components.append(_component(address="direct.example.com", port=8443, protocol="tcp"))
    conf.components.append(_component(address="relay.example.com", port=3001, protocol="tcp"))

    files = _unzip(conf.render())
    assert sorted(files) == ["openvpn-tcp.ovpn", "openvpn-udp.ovpn"]

    udp = files["openvpn-udp.ovpn"]
    assert "remote direct.example.com 1194 udp" in udp
    assert "remote relay.example.com 3000 udp" in udp
    assert " tcp" not in udp

    tcp = files["openvpn-tcp.ovpn"]
    assert "remote direct.example.com 8443 tcp" in tcp
    assert "remote relay.example.com 3001 tcp" in tcp
    assert " udp" not in tcp

    for content in files.values():
        assert content.count("<ca>") == 1
        assert content.count("<auth-user-pass>") == 1


def test_instances_from_a_foreign_pki_are_dropped():
    conf = OpenVPNConfiguration()
    conf.components.append(_component(address="ours.example.com", protocol="udp"))
    conf.components.append(_component(address="theirs.example.com", protocol="udp", ca_cert="OTHER"))

    files = _unzip(conf.render())
    assert "theirs.example.com" not in files["openvpn-udp.ovpn"]
    assert "ours.example.com" in files["openvpn-udp.ovpn"]


def test_tuning_is_handed_to_the_client_and_uses_the_tightest_value():
    """A file must survive its most constrained remote, so the smallest wins."""
    conf = OpenVPNConfiguration()
    conf.components.append(_component(address="direct.example.com", protocol="udp", port=1194, tun_mtu=0, mssfix=1400))
    conf.components.append(_component(address="relay.example.com", protocol="udp", port=3000, tun_mtu=1300, fragment=1300, mssfix=1300))

    udp = _unzip(conf.render())["openvpn-udp.ovpn"]
    assert "tun-mtu 1300" in udp
    assert "mssfix 1300" in udp
    assert "fragment 1300" in udp


def test_fragment_never_reaches_a_tcp_file():
    conf = OpenVPNConfiguration()
    conf.components.append(_component(address="relay.example.com", protocol="tcp", port=3001, tun_mtu=1300, fragment=1300))

    tcp = _unzip(conf.render())["openvpn-tcp.ovpn"]
    assert "tun-mtu 1300" in tcp
    assert "fragment" not in tcp


def test_untuned_configs_stay_untouched():
    conf = OpenVPNConfiguration()
    conf.components.append(_component(address="plain.example.com", protocol="udp", port=1194))

    udp = _unzip(conf.render())["openvpn-udp.ovpn"]
    assert "tun-mtu" not in udp
    assert "fragment" not in udp
    assert "mssfix" not in udp
