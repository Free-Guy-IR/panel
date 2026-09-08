import pytest

from app.db.crud.user import build_revoked_proxy_settings
from app.models.proxy import ProxyTable
from app.utils import l2tp as l2tp_utils, mtproto as mtproto_utils


class _User:
    def __init__(self, settings):
        self.proxy_settings = settings


def _settings(**over):
    base = {
        "vmess": {"id": "b7b0f6ee-3e1b-4a9c-9d8e-4a0e1f2c3d4e"},
        "trojan": {"password": "old-trojan-password"},
        "shadowsocks": {"password": "old-shadowsocks-password", "method": "aes-256-gcm"},
        "wireguard": {"public_key": "old-wg-pub", "peer_ips": ["10.0.0.2/32"]},
        "mtproto": {"secret": "ab" * 16},
        "l2tp": {"password": "old-l2tp-password"},
        "openvpn": {"password": "old-openvpn-password"},
    }
    base.update(over)
    return base


def test_revoking_clears_the_l2tp_and_mtproto_credentials():
    revoked = build_revoked_proxy_settings(_User(_settings()))
    assert revoked["l2tp"]["password"] is None
    assert revoked["mtproto"]["secret"] is None
    assert revoked["openvpn"]["password"] is None
    assert revoked["trojan"]["password"] != "old-trojan-password"
    assert revoked["shadowsocks"]["method"] == "aes-256-gcm"
    assert revoked["wireguard"]["peer_ips"] == ["10.0.0.2/32"]


@pytest.mark.asyncio
async def test_a_revoked_user_with_access_is_issued_fresh_keys(monkeypatch):
    async def has_l2tp(db, groups):
        return True

    async def has_mtproto(db, groups):
        return True

    monkeypatch.setattr(l2tp_utils, "user_has_l2tp_access", has_l2tp)
    monkeypatch.setattr(mtproto_utils, "user_has_mtproto_access", has_mtproto)

    old = _settings()
    revoked = ProxyTable.model_validate(build_revoked_proxy_settings(_User(old)))
    revoked = await l2tp_utils.prepare_l2tp_password(None, revoked, [])
    revoked = await mtproto_utils.prepare_mtproto_secret(None, revoked, [])

    assert revoked.l2tp.password
    assert revoked.l2tp.password != old["l2tp"]["password"]
    assert len(revoked.l2tp.password) == l2tp_utils.L2TP_PASSWORD_LENGTH
    assert revoked.mtproto.secret
    assert revoked.mtproto.secret != old["mtproto"]["secret"]


@pytest.mark.asyncio
async def test_a_revoked_user_without_access_gets_nothing(monkeypatch):
    async def no_access(db, groups):
        return False

    monkeypatch.setattr(l2tp_utils, "user_has_l2tp_access", no_access)
    monkeypatch.setattr(mtproto_utils, "user_has_mtproto_access", no_access)

    revoked = ProxyTable.model_validate(build_revoked_proxy_settings(_User(_settings())))
    revoked = await l2tp_utils.prepare_l2tp_password(None, revoked, [])
    revoked = await mtproto_utils.prepare_mtproto_secret(None, revoked, [])
    assert revoked.l2tp.password is None
    assert revoked.mtproto.secret is None
