from datetime import UTC, datetime

import pytest

from app.fork.traffic_log.parse import SingboxFlows, parse_access_line, parse_singbox_line

NOW = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)


def _pair(cid, inbound_line, outbound_line, flows=None):
    flows = flows or SingboxFlows()
    assert parse_singbox_line(inbound_line, 63, NOW, flows) is None
    return parse_singbox_line(outbound_line, 63, NOW, flows), flows


def test_a_singbox_connection_becomes_one_event_when_its_outbound_is_logged():
    event, flows = _pair(
        "3355022874",
        "+0330 2026-09-17 06:00:00 INFO [3355022874 0ms] inbound/vless[vless-in]: [4242] inbound connection to example.com:443",
        "+0330 2026-09-17 06:00:00 INFO [3355022874 1ms] outbound/direct[direct]: outbound connection to example.com:443",
    )
    assert event is not None
    assert event.user_id == 4242
    assert event.user_label is None
    assert event.inbound == "vless-in"
    assert event.host == "example.com"
    assert event.port == 443
    assert event.protocol == "tcp"
    assert event.route == "DIRECT"
    assert event.refused is False
    assert len(flows) == 0


def test_a_blocked_singbox_packet_connection_is_refused_and_udp():
    event, _ = _pair(
        "99",
        "+0330 2026-09-17 06:00:01 INFO [99 0ms] inbound/hysteria2[hy2]: [77] inbound packet connection to 8.8.8.8:53",
        "+0330 2026-09-17 06:00:01 INFO [99 0ms] outbound/block[block]: outbound connection to 8.8.8.8:53",
    )
    assert event is not None
    assert event.protocol == "udp"
    assert event.route == "BLOCK"
    assert event.refused is True


def test_a_rejecting_outbound_counts_as_refused_even_under_another_tag():
    event, _ = _pair(
        "5",
        "+0330 INFO [5 0ms] inbound/trojan[tr]: [9] inbound connection to ads.example:80",
        "+0330 INFO [5 0ms] outbound/reject[no-ads]: outbound connection to ads.example:80",
    )
    assert event is not None
    assert event.route == "NO-ADS"
    assert event.refused is True


def test_an_ipv6_destination_and_a_named_user_survive():
    event, _ = _pair(
        "7",
        "+0330 INFO [7 0ms] inbound/vmess[vm]: [someone] inbound connection to [2606:4700:4700::1111]:443",
        "+0330 INFO [7 0ms] outbound/direct[direct]: outbound connection to [2606:4700:4700::1111]:443",
    )
    assert event is not None
    assert event.host == "2606:4700:4700::1111"
    assert event.user_id is None
    assert event.user_label == "someone"


def test_an_outbound_without_its_inbound_is_ignored():
    flows = SingboxFlows()
    line = "+0330 INFO [123 0ms] outbound/direct[direct]: outbound connection to example.com:443"
    assert parse_singbox_line(line, 63, NOW, flows) is None


def test_the_pending_table_is_bounded_and_drops_the_oldest():
    flows = SingboxFlows(cap=2)
    for cid in (1, 2, 3):
        parse_singbox_line(
            f"+0330 INFO [{cid} 0ms] inbound/vless[in]: [1] inbound connection to a.example:443", 63, NOW, flows
        )
    assert len(flows) == 2
    assert parse_singbox_line("+0330 INFO [1 0ms] outbound/direct[direct]: x", 63, NOW, flows) is None
    assert parse_singbox_line("+0330 INFO [3 0ms] outbound/direct[direct]: x", 63, NOW, flows) is not None


def test_an_xray_line_is_still_parsed_by_the_xray_parser_only():
    line = "from 1.2.3.4:5 accepted tcp:example.com:443 [in -> DIRECT] email: 4242"
    assert parse_access_line(line, 7, NOW) is not None
    assert parse_singbox_line(line, 7, NOW, SingboxFlows()) is None


def test_a_singbox_line_is_not_mistaken_for_an_xray_line():
    line = "+0330 INFO [1 0ms] inbound/vless[in]: [4242] inbound connection to example.com:443"
    assert parse_access_line(line, 63, NOW) is None


@pytest.mark.asyncio
async def test_the_live_feed_refuses_more_viewers_than_it_can_serve():
    import contextlib as _contextlib

    from app.fork.traffic_log.collector import SUBSCRIBER_CAP, TrafficCollector

    collector = TrafficCollector()
    collector.available = True
    collector.enabled = True

    async with _contextlib.AsyncExitStack() as stack:
        queues = [await stack.enter_async_context(collector.subscribe(node_id=1)) for _ in range(SUBSCRIBER_CAP)]
        assert all(queue.empty() for queue in queues)
        assert len(collector._subscribers) == SUBSCRIBER_CAP

        refused = await stack.enter_async_context(collector.subscribe(node_id=1))
        control = refused.get_nowait()
        assert control["control"] == "too_many_viewers"
        assert len(collector._subscribers) == SUBSCRIBER_CAP

    assert len(collector._subscribers) == 0
