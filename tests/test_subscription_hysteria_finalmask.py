import json
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.hosts import _prepare_subscription_inbound_data
from app.models.host import BaseHost, FinalMask
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription.clash import ClashMetaConfiguration
from app.subscription.links import StandardLinks
from app.subscription.singbox import SingBoxConfiguration

FINALMASK_DICT = {
    "udp": [{"type": "salamander", "settings": {"password": "obfs-pw"}}],
    "quicParams": {"brutalUp": "50 mbps", "brutalDown": "100 mbps"},
}


def _model_finalmask() -> FinalMask:
    return FinalMask.model_validate(FINALMASK_DICT)


def _inbound(protocol: str, finalmask) -> SubscriptionInboundData:
    return SubscriptionInboundData(
        remark="t",
        inbound_tag="T",
        protocol=protocol,
        address="edge.example.com",
        port=443,
        network="tcp",
        tls_config=TLSConfig(tls="tls", sni="cert.example.com"),
        transport_config=TCPTransportConfig(),
        finalmask=finalmask,
        priority=0,
    )


def test_links_hysteria2_host_with_finalmask_model_renders_obfs():
    links = StandardLinks()
    links.add("hy2", "edge.example.com", _inbound("hysteria2", _model_finalmask()), {"password": "SECRET"})

    rendered = links.render()
    assert "obfs=salamander" in rendered
    assert "obfs-password=obfs-pw" in rendered


def test_links_hysteria_host_with_finalmask_model_renders_obfs():
    links = StandardLinks()
    links.add("hy", "edge.example.com", _inbound("hysteria", _model_finalmask()), {"auth": "SECRET"})

    rendered = links.render()
    assert "obfs-password=obfs-pw" in rendered


def test_clash_meta_hysteria2_host_with_finalmask_model_renders_obfs():
    meta = ClashMetaConfiguration()
    meta.add("hy2", "edge.example.com", _inbound("hysteria2", _model_finalmask()), {"password": "SECRET"})

    node = meta.data["proxies"][0]
    assert node["type"] == "hysteria2"
    assert node["obfs"] == "salamander"
    assert node["obfs-password"] == "obfs-pw"
    assert node["up"].lower() == "50 mbps"
    assert node["down"].lower() == "100 mbps"


def test_singbox_hysteria2_host_with_finalmask_model_renders_obfs():
    conf = SingBoxConfiguration()
    conf.add("hy2", "edge.example.com", _inbound("hysteria2", _model_finalmask()), {"password": "SECRET"})

    outbound = conf.config["outbounds"][0]
    assert outbound["type"] == "hysteria2"
    assert outbound["obfs"] == {"type": "salamander", "password": "obfs-pw"}


def test_hysteria2_host_with_plain_dict_finalmask_still_renders():
    links = StandardLinks()
    links.add("hy2", "edge.example.com", _inbound("hysteria2", dict(FINALMASK_DICT)), {"password": "SECRET"})

    assert "obfs-password=obfs-pw" in links.render()


SALAMANDER = {"type": "salamander", "settings": {"password": "obfs-secret"}}


@pytest.fixture
def hysteria_inbound(monkeypatch: pytest.MonkeyPatch):
    inbound_config = {"protocol": "hysteria", "network": "tcp", "tls": "tls", "sni": ["cert.example.com"]}
    monkeypatch.setattr("app.core.hosts.core_manager.get_inbound_by_tag", AsyncMock(return_value=inbound_config))
    return inbound_config


async def _prepare(**host_fields) -> SubscriptionInboundData:
    host = BaseHost(remark="hy2", address={"edge.example.com"}, port=443, inbound_tag="hy2", priority=0, **host_fields)
    inbound = await _prepare_subscription_inbound_data(host)
    return inbound.model_copy(update={"port": inbound.port[0]})


def _nats_round_trip(inbound: SubscriptionInboundData) -> SubscriptionInboundData:
    return SubscriptionInboundData.model_validate(json.loads(json.dumps(inbound.model_dump())))


def _link_query(inbound: SubscriptionInboundData) -> dict[str, list[str]]:
    links = StandardLinks()
    links.add("hy2", "edge.example.com", inbound, {"auth": "auth-password"})
    return parse_qs(urlparse(links.links[0]).query)


# Port hopping without a udp mask: the shape a plain FinalMask | dict union turned back into a model
QUIC_ONLY_HOP = {"quicParams": {"udpHop": {"ports": "20000-20010", "interval": "30"}}}


def _build_hysteria2(inbound: SubscriptionInboundData) -> tuple[dict[str, list[str]], dict, dict]:
    """Share link query, Clash Meta proxy and sing-box outbound of one host."""
    meta = ClashMetaConfiguration()
    meta.add("hy2", "edge.example.com", inbound, {"auth": "auth-password"})
    singbox = SingBoxConfiguration()
    singbox.add("hy2", "edge.example.com", inbound, {"auth": "auth-password"})
    return _link_query(inbound), meta.data["proxies"][0], singbox.config["outbounds"][-1]


def _assert_builders_read_quic_only_hop(inbound: SubscriptionInboundData):
    """udpHop reaches every builder, and no obfs appears without a salamander layer."""
    query, node, outbound = _build_hysteria2(inbound)
    assert query["mports"] == ["20000-20010"]
    assert "obfs" not in query
    assert node["ports"] == "20000-20010"
    assert node["hop-interval"] == "30s"
    assert not node.get("obfs")
    assert outbound["server_ports"] == ["20000:20010"]
    assert outbound["hop_interval"] == "30s"
    assert "obfs" not in outbound


@pytest.mark.usefixtures("hysteria_inbound")
async def test_prepared_finalmask_stays_a_dict_with_xray_field_names():
    inbound = await _prepare(final_mask_settings=QUIC_ONLY_HOP)
    assert type(inbound.finalmask) is dict
    assert inbound.finalmask == QUIC_ONLY_HOP

    inbound.finalmask = FinalMask.model_validate(QUIC_ONLY_HOP)
    assert type(inbound.finalmask) is dict
    assert inbound.finalmask == QUIC_ONLY_HOP


@pytest.mark.usefixtures("hysteria_inbound")
async def test_hysteria2_builders_read_host_quic_params_without_udp_mask():
    _assert_builders_read_quic_only_hop(await _prepare(final_mask_settings=QUIC_ONLY_HOP))


@pytest.mark.usefixtures("hysteria_inbound")
async def test_hysteria2_builders_read_host_quic_params_without_udp_mask_after_nats_cache_round_trip():
    _assert_builders_read_quic_only_hop(_nats_round_trip(await _prepare(final_mask_settings=QUIC_ONLY_HOP)))


async def test_hysteria2_builders_read_inbound_quic_params_without_udp_mask(hysteria_inbound):
    hysteria_inbound["finalmask"] = QUIC_ONLY_HOP

    _assert_builders_read_quic_only_hop(await _prepare())


@pytest.mark.parametrize(
    ("finalmask", "brutal_up", "obfs"),
    [
        pytest.param({"quicParams": {"brutalUp": "50 mbps"}}, "50 mbps", None, id="brutal-only"),
        pytest.param(
            {"udp": [SALAMANDER], "quicParams": {"brutalUp": "50 mbps"}}, "50 mbps", "salamander", id="no-udphop"
        ),
        pytest.param({"quicParams": {}}, None, None, id="empty-quic-params"),
    ],
)
@pytest.mark.usefixtures("hysteria_inbound")
async def test_hysteria2_builders_accept_host_finalmask_without_udphop(finalmask, brutal_up, obfs):
    query, node, outbound = _build_hysteria2(await _prepare(final_mask_settings=finalmask))

    assert query.get("obfs") == ([obfs] if obfs else None)
    assert "mports" not in query
    assert not node.get("ports")
    assert node.get("up") == brutal_up
    assert "server_ports" not in outbound


@pytest.mark.parametrize(
    "finalmask",
    [
        pytest.param({"quicParams": {"udpHop": None}}, id="null-udphop"),
        pytest.param({"udp": [{"type": "salamander", "settings": None}], "quicParams": None}, id="null-sections"),
    ],
)
async def test_hysteria2_builders_accept_nulls_in_inbound_finalmask(hysteria_inbound, finalmask):
    # Core configs are free-form JSON, so optional sections may be written as null
    hysteria_inbound["finalmask"] = finalmask

    query, node, outbound = _build_hysteria2(await _prepare())

    assert "mports" not in query
    assert "obfs" not in query
    assert not node.get("ports")
    assert "server_ports" not in outbound
