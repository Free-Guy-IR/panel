import pytest

from app.core import hosts as hosts_module
from app.db.models import ProxyHostFingerprint
from app.models.host import BaseHost


def reality_inbound(fp):
    return {
        "protocol": "vless",
        "network": "tcp",
        "tls": "reality",
        "port": 443,
        "sni": ["example.com"],
        "pbk": "public-key",
        "sid": "abcd",
        "fp": fp,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("inbound_fp", ["firefox", "safari"])
async def test_default_host_fingerprint_inherits_inbound_fp(monkeypatch, inbound_fp):
    async def get_inbound_by_tag(tag):
        return reality_inbound(inbound_fp)

    monkeypatch.setattr(hosts_module.core_manager, "get_inbound_by_tag", get_inbound_by_tag)
    host = BaseHost(remark="r", inbound_tag="vless-reality", priority=1)
    assert host.fingerprint == ProxyHostFingerprint.none

    data = await hosts_module._prepare_subscription_inbound_data(host)

    assert data.tls_config.fingerprint == inbound_fp


@pytest.mark.asyncio
async def test_explicit_host_fingerprint_wins_over_inbound_fp(monkeypatch):
    async def get_inbound_by_tag(tag):
        return reality_inbound("firefox")

    monkeypatch.setattr(hosts_module.core_manager, "get_inbound_by_tag", get_inbound_by_tag)
    host = BaseHost(remark="r", inbound_tag="vless-reality", priority=1, fingerprint=ProxyHostFingerprint.safari)

    data = await hosts_module._prepare_subscription_inbound_data(host)

    assert data.tls_config.fingerprint == "safari"


@pytest.mark.asyncio
async def test_default_host_without_inbound_fp_keeps_reality_chrome_fallback(monkeypatch):
    async def get_inbound_by_tag(tag):
        return reality_inbound(None)

    monkeypatch.setattr(hosts_module.core_manager, "get_inbound_by_tag", get_inbound_by_tag)
    host = BaseHost(remark="r", inbound_tag="vless-reality", priority=1)

    data = await hosts_module._prepare_subscription_inbound_data(host)

    assert data.tls_config.fingerprint == "chrome"
