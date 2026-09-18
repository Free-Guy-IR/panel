import pytest

from app.fork.content_filter import rules, service

MATCHER_PREFIXED = [
    "regexp:^.{0,}$",
    "ext:pgfilter.dat:anything",
    "keyword:example.com",
    "geosite:netflix.com",
    "full:example.com",
    "domain:example.com",
    "=regexp:^.{0,}$",
]


PRODUCTION_RULES = [
    {"type": "field", "port": "25", "outboundTag": "BLOCK"},
    {"type": "field", "ip": ["geoip:facebook"], "port": "443", "network": "udp", "outboundTag": "BLOCK"},
    {
        "type": "field",
        "domain": ["keyword:instagram", "domain:fbcdn.net", "domain:wikipedia.org"],
        "outboundTag": "DIRECT",
    },
    {"type": "field", "ip": ["100.64.0.0/10"], "outboundTag": "DIRECT"},
    {"type": "field", "ip": ["geoip:private"], "outboundTag": "BLOCK"},
    {"type": "field", "inboundTag": ["india"], "outboundTag": "india_wireguard"},
    {"type": "field", "inboundTag": ["adsblock"], "outboundTag": "adsblock_wireguard"},
    {"type": "field", "inboundTag": ["egypt"], "outboundTag": "egypt_wireguard"},
    {
        "type": "field",
        "domain": ["geosite:google-gemini", "domain:accounts.google.com"],
        "inboundTag": ["spof 401", "vless80", "VLESS HTTPUPGRADE 8080", "VLESS TCP 2526", "test"],
        "outboundTag": "gemini-usa",
    },
]

PRODUCTION_CORE = {
    "outbounds": [
        {"tag": "DIRECT", "protocol": "freedom"},
        {"tag": "BLOCK", "protocol": "blackhole"},
        {"tag": "gemini-usa", "protocol": "vless"},
        {"tag": "india_wireguard", "protocol": "wireguard"},
        {"tag": "adsblock_wireguard", "protocol": "wireguard"},
        {"tag": "egypt_wireguard", "protocol": "wireguard"},
    ],
    "routing": {"domainStrategy": "IPIfNonMatch", "rules": PRODUCTION_RULES},
}


def _own_concrete_domains(filter_rules: list[dict]) -> set[str]:
    found: set[str] = set()
    for rule in filter_rules:
        for matcher in rule.get("domain") or []:
            kind, _, rest = matcher.partition(":")
            if kind in ("domain", "full"):
                found.add(rest)
    return found


def _named(result) -> list[str]:
    return [clash.split(" sends ", 1)[0] for clash in result.clashes]


def _quiet(result) -> list[str]:
    return result.clashes + result.advisories


def _block_only(assignment_id: int, inbound: str, blocked: str) -> list[dict]:
    return rules.build_rules(
        assignment_id=assignment_id,
        inbound_tags=[inbound],
        categories=[],
        allow_list=[],
        block_list=[blocked],
        strict_mode=False,
    )


def test_a_core_tagging_its_outbounds_in_lower_case_is_reported_as_missing_both():
    config = {
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "blocked", "protocol": "blackhole"},
        ]
    }

    assert rules.missing_outbounds(config) == ["BLOCK", "DIRECT"]


def test_a_core_carrying_the_tags_the_rules_reference_is_reported_complete():
    config = {
        "outbounds": [
            {"tag": "DIRECT", "protocol": "freedom"},
            {"tag": "BLOCK", "protocol": "blackhole"},
        ]
    }

    assert rules.missing_outbounds(config) == []


def test_a_core_with_no_outbounds_at_all_is_reported_as_missing_everything():
    assert rules.missing_outbounds({}) == ["BLOCK", "DIRECT"]


def test_every_outbound_the_builder_emits_is_covered_by_the_missing_check():
    built = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=["adult"],
        allow_list=["allowed.example"],
        block_list=["blocked.example"],
        strict_mode=True,
    )
    emitted = {rule["outboundTag"] for rule in built}

    assert emitted == {"BLOCK", "DIRECT"}
    assert emitted == set(rules.missing_outbounds({"outbounds": []}))


def test_the_real_blackhole_and_freedom_outbounds_are_found_by_protocol():
    config = {
        "outbounds": [
            {"tag": "proxy", "protocol": "vmess"},
            {"tag": "quarantine", "protocol": "blackhole"},
            {"tag": "escape", "protocol": "freedom"},
        ]
    }

    assert rules.resolve_outbound_tags(config) == {"block": "quarantine", "direct": "escape"}


def test_the_blackhole_resolver_picks_the_same_tag_the_node_would_pick():
    config = {
        "outbounds": [
            {"tag": "zeta", "protocol": "blackhole"},
            {"tag": "alpha", "protocol": "blackhole"},
        ]
    }

    assert rules.resolve_outbound_tags(config)["block"] == "alpha"


@pytest.mark.parametrize("entry", MATCHER_PREFIXED)
def test_a_matcher_prefix_in_an_allow_list_is_refused_instead_of_passed_through(entry: str):
    with pytest.raises(rules.RuleValueError):
        rules.build_rules(
            assignment_id=1,
            inbound_tags=["in"],
            categories=[],
            allow_list=[entry],
            block_list=[],
            strict_mode=False,
        )


@pytest.mark.parametrize("entry", MATCHER_PREFIXED)
def test_a_matcher_prefix_in_a_block_list_is_refused_instead_of_passed_through(entry: str):
    with pytest.raises(rules.RuleValueError):
        rules.build_rules(
            assignment_id=1,
            inbound_tags=["in"],
            categories=[],
            allow_list=[],
            block_list=[entry],
            strict_mode=False,
        )


def test_a_regexp_allow_entry_can_no_longer_neutralise_the_whole_profile():
    with pytest.raises(rules.RuleValueError):
        rules.build_rules(
            assignment_id=1,
            inbound_tags=["in"],
            categories=["adult"],
            allow_list=["regexp:^.{0,}$"],
            block_list=[],
            strict_mode=True,
        )


def test_a_malformed_entry_is_refused_rather_than_silently_dropped():
    with pytest.raises(rules.RuleValueError):
        rules.build_rules(
            assignment_id=1,
            inbound_tags=["in"],
            categories=[],
            allow_list=["no-dot-here"],
            block_list=[],
            strict_mode=False,
        )


def test_only_vetted_matcher_prefixes_reach_the_emitted_config():
    built = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=["adult"],
        allow_list=["=allowed.example", "*.wild.example"],
        block_list=["blocked.example"],
        strict_mode=True,
    )
    seen = {matcher.split(":", 1)[0] for rule in built for matcher in rule.get("domain") or []}

    assert seen
    assert seen <= set(rules.ALLOWED_MATCHER_PREFIXES)


def test_the_documented_domain_shorthands_still_work():
    built = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["=exact.example", "*.wild.example", "Plain.Example."],
        block_list=[],
        strict_mode=False,
    )

    assert built[0]["domain"] == ["full:exact.example", "domain:wild.example", "domain:plain.example"]


def test_an_internationalised_domain_is_still_accepted():
    built = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["نمونه.ایران"],
        block_list=[],
        strict_mode=False,
    )

    assert built[0]["domain"] == ["domain:نمونه.ایران"]


def test_an_operator_rule_that_merely_starts_with_the_prefix_is_not_claimed():
    operator_rule = {"ruleTag": "pgcf-legacy-manual", "outboundTag": "special"}

    assert rules.owns_tag("pgcf-legacy-manual") is False
    assert service._strip_owned([operator_rule]) == [operator_rule]


def test_a_tag_the_builder_generated_is_claimed():
    for rule in _block_only(3, "in", "blocked.example"):
        assert rules.owns_tag(rule["ruleTag"]) is True


def test_a_forged_tag_that_copies_the_shape_but_not_the_checksum_is_not_claimed():
    real = _block_only(3, "in", "blocked.example")[0]["ruleTag"]
    forged = real[:-1] + ("0" if real[-1] != "0" else "1")

    assert real != forged
    assert rules.owns_tag(forged) is False


def test_two_assignments_reusing_one_id_do_not_share_a_rule_tag():
    first = {rule["ruleTag"] for rule in _block_only(7, "in-a", "a.example")}
    second = {rule["ruleTag"] for rule in _block_only(7, "in-b", "b.example")}

    assert first and second
    assert first.isdisjoint(second)


def test_the_same_assignment_always_regenerates_the_same_tags():
    assert _block_only(7, "in-a", "a.example") == _block_only(7, "in-a", "a.example")


def test_an_orphan_from_a_previous_assignment_is_distinguishable_by_identity():
    orphan = _block_only(7, "in-a", "a.example")[0]["ruleTag"]
    current = _block_only(7, "in-b", "b.example")[0]["ruleTag"]

    assert rules.tag_assignment_id(orphan) == rules.tag_assignment_id(current) == 7
    assert rules.tag_identity(orphan) != rules.tag_identity(current)


def test_a_pre_existing_blackhole_rule_is_not_reported_as_a_clash():
    existing = [{"ruleTag": "op-1", "inboundTag": ["in"], "outboundTag": "blackhole"}]

    assert _quiet(rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "blocked.example"))) == []


def test_the_core_config_decides_which_outbound_really_drops_traffic():
    config = {
        "outbounds": [
            {"tag": "quarantine", "protocol": "blackhole"},
            {"tag": "exit", "protocol": "freedom"},
        ]
    }
    dropped = [{"ruleTag": "op-1", "inboundTag": ["in"], "outboundTag": "quarantine"}]
    leaked = [{"ruleTag": "op-2", "inboundTag": ["in"], "outboundTag": "exit"}]

    assert _quiet(rules.conflicting_rules(dropped, ["in"], config, _block_only(1, "in", "blocked.example"))) == []
    assert _named(rules.conflicting_rules(leaked, ["in"], config, _block_only(1, "in", "blocked.example"))) == ["op-2"]


def test_a_rule_that_carries_the_traffic_away_is_still_a_clash():
    existing = [{"ruleTag": "op-1", "inboundTag": ["in"], "outboundTag": "DIRECT"}]

    assert _named(rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "blocked.example"))) == ["op-1"]


def test_a_rule_on_another_inbound_is_still_not_a_clash():
    existing = [{"ruleTag": "op-1", "inboundTag": ["other"], "outboundTag": "DIRECT"}]

    assert _quiet(rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "blocked.example"))) == []


def test_the_filters_own_rules_are_never_reported_as_clashing_with_themselves():
    own = _block_only(4, "in", "blocked.example")

    assert _quiet(rules.conflicting_rules(own, ["in"], None, own)) == []


def test_a_bare_catch_all_with_a_non_harmless_outbound_is_reported():
    existing = [{"type": "field", "ruleTag": "op-1", "outboundTag": "leak"}]
    config = {"outbounds": [{"tag": "BLOCK", "protocol": "blackhole"}], "routing": {"rules": existing}}

    clashes = rules.conflicting_rules(existing, ["in"], config, _block_only(1, "in", "blocked.example"))

    assert _named(clashes) == ["op-1"]
    assert "every connection" in clashes.clashes[0]


def test_an_unscoped_operator_rule_reaches_every_inbound_of_the_core():
    existing = [{"type": "field", "ruleTag": "op-1", "outboundTag": "leak"}]
    config = {"outbounds": [{"tag": "BLOCK", "protocol": "blackhole"}], "routing": {"rules": existing}}
    own = _block_only(1, "in", "blocked.example")

    assert _named(rules.conflicting_rules(existing, ["in"], config, own)) == ["op-1"]
    assert _named(rules.conflicting_rules(existing, [], config, own)) == ["op-1"]


def test_an_unscoped_filter_is_compared_against_every_pre_existing_rule():
    existing = [{"type": "field", "ruleTag": "op-1", "inboundTag": ["somewhere-else"], "outboundTag": "leak"}]
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=[],
        categories=[],
        allow_list=[],
        block_list=["blocked.example"],
        strict_mode=False,
    )

    assert _named(rules.conflicting_rules(existing, [], None, own)) == ["op-1"]


def test_a_direct_rule_whose_domains_cannot_match_the_filter_is_not_reported():
    existing = [
        {
            "type": "field",
            "ruleTag": "op-1",
            "domain": ["domain:wikipedia.org", "full:example.com"],
            "outboundTag": "DIRECT",
        }
    ]

    assert _quiet(rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "pornhub.com"))) == []


def test_a_direct_rule_whose_domains_do_match_the_filter_is_reported():
    existing = [{"type": "field", "ruleTag": "op-1", "domain": ["domain:pornhub.com"], "outboundTag": "DIRECT"}]

    clashes = rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "pornhub.com"))

    assert _named(clashes) == ["op-1"]
    assert "domain:pornhub.com" in clashes.clashes[0]


def test_a_parent_domain_of_a_blocked_host_is_reported():
    existing = [{"type": "field", "ruleTag": "op-1", "domain": ["domain:com"], "outboundTag": "DIRECT"}]

    assert _named(rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "pornhub.com"))) == ["op-1"]


def test_a_keyword_rule_against_a_category_we_cannot_expand_advises_and_names_both_sides():
    existing = [{"type": "field", "ruleTag": "op-1", "domain": ["keyword:zzqqxx"], "outboundTag": "DIRECT"}]
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=["adult"],
        allow_list=[],
        block_list=[],
        strict_mode=False,
    )

    result = rules.conflicting_rules(existing, ["in"], None, own)

    assert result.clashes == []
    assert result.advisories == [
        "op-1 could send a hostname matching keyword:zzqqxx against geosite:category-porn to DIRECT"
    ]


def test_the_production_core_reports_only_the_unscoped_direct_rule():
    own = _block_only(1, "quiet-inbound", "instagram.com")

    clashes = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert len(_quiet(clashes)) == 1
    assert "keyword:instagram" in clashes.clashes[0]
    assert clashes.clashes[0].endswith("to DIRECT")


def test_the_production_blocking_rules_are_never_reported():
    own = _block_only(1, "quiet-inbound", "instagram.com")

    clashes = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert not any("to BLOCK" in clash for clash in _quiet(clashes))


def test_the_production_gemini_rule_is_reported_only_on_its_own_inbounds():
    own_quiet = _block_only(1, "quiet-inbound", "instagram.com")
    own_shared = _block_only(1, "vless80", "instagram.com")

    quiet = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own_quiet)
    shared = rules.conflicting_rules(PRODUCTION_RULES, ["vless80"], PRODUCTION_CORE, own_shared)

    assert not any("gemini-usa" in clash for clash in _quiet(quiet))
    assert any("gemini-usa" in clash for clash in _quiet(shared))


def test_the_production_wireguard_rules_are_reported_only_on_their_own_inbound():
    own_quiet = _block_only(1, "quiet-inbound", "instagram.com")
    own_india = _block_only(1, "india", "instagram.com")

    quiet = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own_quiet)
    india = rules.conflicting_rules(PRODUCTION_RULES, ["india"], PRODUCTION_CORE, own_india)

    assert not any("india_wireguard" in clash for clash in _quiet(quiet))
    assert any("india_wireguard" in clash for clash in _quiet(india))
    assert not any("egypt_wireguard" in clash for clash in _quiet(india))


def test_the_production_ip_rule_is_silent_for_a_domain_filter_and_loud_under_strict_mode():
    plain = _block_only(1, "quiet-inbound", "instagram.com")
    strict = rules.build_rules(
        assignment_id=1,
        inbound_tags=["quiet-inbound"],
        categories=[],
        allow_list=[],
        block_list=["instagram.com"],
        strict_mode=True,
    )

    relaxed = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, plain)
    tightened = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, strict)

    assert not any("100.64.0.0/10" in clash for clash in _quiet(relaxed))
    assert any("100.64.0.0/10" in clash for clash in _quiet(tightened))


def test_a_live_listing_stub_is_judged_through_the_core_config():
    own = _block_only(1, "quiet-inbound", "instagram.com")
    stubs = [{"ruleTag": "", "outboundTag": str(rule.get("outboundTag") or "")} for rule in PRODUCTION_RULES]

    clashes = rules.conflicting_rules(stubs, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert len(clashes.clashes) == 1
    assert "keyword:instagram" in clashes.clashes[0]


def test_a_live_rule_the_core_config_cannot_explain_is_reported():
    own = _block_only(1, "quiet-inbound", "instagram.com")
    stubs = [{"ruleTag": "injected", "outboundTag": "somewhere-unknown"}]

    clashes = rules.conflicting_rules(stubs, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert clashes.clashes == ["an unidentified rule sends every connection to somewhere-unknown"]
    assert clashes.advisories == []


def test_the_clash_message_tells_the_operator_what_to_change():
    assert "inboundTag" in rules.CLASH_REMEDY
    assert "appends" in rules.CLASH_REMEDY


def test_an_operator_rule_matching_only_an_allow_list_domain_is_not_reported():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["wikipedia.org"],
        block_list=["pornhub.com"],
        strict_mode=False,
    )
    allowed = [{"type": "field", "ruleTag": "op-1", "domain": ["domain:wikipedia.org"], "outboundTag": "DIRECT"}]
    blocked = [{"type": "field", "ruleTag": "op-2", "domain": ["domain:pornhub.com"], "outboundTag": "DIRECT"}]

    assert _quiet(rules.conflicting_rules(allowed, ["in"], None, own)) == []
    assert _named(rules.conflicting_rules(blocked, ["in"], None, own)) == ["op-2"]


CARRIES_THE_FILTERS_TRAFFIC = [
    ("op-https", {"port": "443"}),
    ("op-quic", {"network": "udp"}),
    ("op-public-ip", {"ip": ["192.88.99.0/24"]}),
    ("op-geoip", {"ip": ["geoip:ir"]}),
    ("op-source", {"source": ["10.0.0.0/8"]}),
    ("op-user", {"user": ["someone@example.com"]}),
    ("op-protocol", {"protocol": ["bittorrent"]}),
]

CANNOT_REACH_A_PUBLIC_DOMAIN = [
    ("op-private", {"ip": ["geoip:private"]}),
    ("op-cgnat", {"ip": ["100.64.0.0/10"]}),
    ("op-rfc1918", {"ip": ["10.0.0.0/8", "192.168.0.0/16"]}),
    ("op-loopback", {"ip": ["127.0.0.1"]}),
]


@pytest.mark.parametrize(("tag", "predicate"), CARRIES_THE_FILTERS_TRAFFIC)
def test_a_rule_matching_on_another_dimension_still_swallows_the_filters_traffic(tag: str, predicate: dict):
    existing = [{"type": "field", "ruleTag": tag, **predicate, "outboundTag": "DIRECT"}]

    clashes = rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "pornhub.com"))

    assert _named(clashes) == [tag]


@pytest.mark.parametrize(("tag", "predicate"), CANNOT_REACH_A_PUBLIC_DOMAIN)
def test_a_rule_confined_to_address_space_that_cannot_serve_a_domain_is_dismissed(tag: str, predicate: dict):
    existing = [{"type": "field", "ruleTag": tag, **predicate, "outboundTag": "DIRECT"}]

    assert _quiet(rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "pornhub.com"))) == []


@pytest.mark.parametrize(("tag", "predicate"), CANNOT_REACH_A_PUBLIC_DOMAIN)
def test_strict_mode_reports_even_the_private_address_rules_it_would_otherwise_dismiss(tag: str, predicate: dict):
    existing = [{"type": "field", "ruleTag": tag, **predicate, "outboundTag": "DIRECT"}]
    strict = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=[],
        block_list=["pornhub.com"],
        strict_mode=True,
    )

    assert _named(rules.conflicting_rules(existing, ["in"], None, strict)) == [tag]


def test_a_rule_carrying_every_https_connection_names_the_port_it_matches():
    existing = [{"type": "field", "ruleTag": "op-https", "port": "443", "outboundTag": "DIRECT"}]

    clashes = rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "pornhub.com"))

    assert clashes.clashes == ["op-https sends every connection matching port 443 to DIRECT"]


def test_a_mixed_predicate_rule_is_dismissed_only_when_its_addresses_are_all_unreachable():
    reachable = [{"type": "field", "ruleTag": "op-1", "ip": ["10.0.0.0/8", "192.88.99.0/24"], "outboundTag": "DIRECT"}]
    unreachable = [
        {"type": "field", "ruleTag": "op-2", "ip": ["10.0.0.0/8", "192.168.0.0/16"], "outboundTag": "DIRECT"}
    ]
    own = _block_only(1, "in", "pornhub.com")

    assert _named(rules.conflicting_rules(reachable, ["in"], None, own)) == ["op-1"]
    assert _quiet(rules.conflicting_rules(unreachable, ["in"], None, own)) == []


def test_an_empty_filter_can_never_clash_with_anything():
    existing = [{"type": "field", "ruleTag": "op-1", "outboundTag": "leak"}]

    assert _quiet(rules.conflicting_rules(existing, ["in"], None, [])) == []


def test_the_check_cannot_run_without_being_told_what_the_filter_matches():
    with pytest.raises(TypeError):
        rules.conflicting_rules([{"ruleTag": "op-1", "outboundTag": "DIRECT"}], ["in"])


def test_a_dns_outbound_is_not_a_bypass_for_a_domain_filter():
    config = {
        "outbounds": [
            {"tag": "DIRECT", "protocol": "freedom"},
            {"tag": "BLOCK", "protocol": "blackhole"},
            {"tag": "dns-out", "protocol": "dns"},
        ]
    }
    existing = [{"type": "field", "ruleTag": "op-dns", "port": "53", "outboundTag": "dns-out"}]

    assert _quiet(rules.conflicting_rules(existing, ["in"], config, _block_only(1, "in", "pornhub.com"))) == []
    assert _quiet(rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "pornhub.com"))) == []


MISLEADING_BLOCK_CORE = {
    "outbounds": [
        {"tag": "BLOCK", "protocol": "freedom"},
        {"tag": "void", "protocol": "blackhole"},
        {"tag": "DIRECT", "protocol": "freedom"},
    ]
}


def test_an_outbound_named_block_that_is_really_freedom_is_not_exempt():
    existing = [
        {
            "type": "field",
            "ruleTag": "op-fake",
            "domain": ["domain:pornhub.com"],
            "inboundTag": ["in"],
            "outboundTag": "BLOCK",
        }
    ]

    clashes = rules.conflicting_rules(existing, ["in"], MISLEADING_BLOCK_CORE, _block_only(1, "in", "pornhub.com"))

    assert _named(clashes) == ["op-fake"]


def test_the_cores_real_blackhole_is_exempt_whatever_it_is_called():
    existing = [
        {
            "type": "field",
            "ruleTag": "op-real",
            "domain": ["domain:pornhub.com"],
            "inboundTag": ["in"],
            "outboundTag": "void",
        }
    ]

    assert (
        _quiet(rules.conflicting_rules(existing, ["in"], MISLEADING_BLOCK_CORE, _block_only(1, "in", "pornhub.com")))
        == []
    )


def test_outbound_exemptions_are_case_sensitive_when_the_core_config_is_known():
    config = {"outbounds": [{"tag": "block", "protocol": "blackhole"}, {"tag": "DIRECT", "protocol": "freedom"}]}
    own = _block_only(1, "in", "pornhub.com")
    exact = [{"type": "field", "ruleTag": "op-1", "inboundTag": ["in"], "outboundTag": "block"}]
    different_case = [{"type": "field", "ruleTag": "op-2", "inboundTag": ["in"], "outboundTag": "BLOCK"}]

    assert _quiet(rules.conflicting_rules(exact, ["in"], config, own)) == []
    assert _named(rules.conflicting_rules(different_case, ["in"], config, own)) == ["op-2"]


def test_the_api_handler_is_exempt_only_while_it_is_not_a_real_outbound():
    own = _block_only(1, "in", "pornhub.com")
    handler = {"outbounds": [{"tag": "BLOCK", "protocol": "blackhole"}]}
    impostor = {"outbounds": [{"tag": "BLOCK", "protocol": "blackhole"}, {"tag": "api", "protocol": "freedom"}]}
    existing = [{"type": "field", "ruleTag": "op-1", "inboundTag": ["in"], "outboundTag": "api"}]

    assert _quiet(rules.conflicting_rules(existing, ["in"], handler, own)) == []
    assert _named(rules.conflicting_rules(existing, ["in"], impostor, own)) == ["op-1"]


def test_the_name_hint_fallback_still_applies_when_no_core_config_is_available():
    own = _block_only(1, "in", "pornhub.com")
    blackhole = [{"type": "field", "ruleTag": "op-1", "inboundTag": ["in"], "outboundTag": "blackhole"}]
    leaking = [{"type": "field", "ruleTag": "op-2", "inboundTag": ["in"], "outboundTag": "somewhere"}]

    assert _quiet(rules.conflicting_rules(blackhole, ["in"], None, own)) == []
    assert _named(rules.conflicting_rules(leaking, ["in"], None, own)) == ["op-2"]


@pytest.mark.parametrize("keyword", ["keyword:ads", "keyword:instagram", "keyword:zzqqxx"])
def test_a_keyword_rule_against_an_unrelated_subtree_advises_without_blocking(keyword: str):
    existing = [{"type": "field", "ruleTag": "op-kw", "domain": [keyword], "outboundTag": "DIRECT"}]

    result = rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "example.com"))

    assert result.clashes == []
    assert result.advisories == [f"op-kw could send a hostname matching {keyword} to DIRECT"]


def test_a_keyword_that_really_matches_a_blocked_domain_still_blocks():
    existing = [{"type": "field", "ruleTag": "op-kw", "domain": ["keyword:example"], "outboundTag": "DIRECT"}]

    result = rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "example.com"))

    assert _named(result) == ["op-kw"]
    assert result.advisories == []


def test_a_keyword_that_a_category_expands_to_a_real_domain_for_still_blocks():
    existing = [{"type": "field", "ruleTag": "op-kw", "domain": ["keyword:instagram"], "outboundTag": "DIRECT"}]
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=["social_network"],
        allow_list=[],
        block_list=[],
        strict_mode=False,
    )

    result = rules.conflicting_rules(existing, ["in"], None, own)

    assert _named(result) == ["op-kw"]
    assert result.advisories == []
    assert _own_concrete_domains(own).issuperset({"instagram.com"})


def test_a_keyword_is_still_decided_exactly_against_an_exact_match_filter():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=[],
        block_list=["=pornhub.com"],
        strict_mode=False,
    )
    misses = [{"type": "field", "ruleTag": "op-1", "domain": ["keyword:ads"], "outboundTag": "DIRECT"}]
    hits = [{"type": "field", "ruleTag": "op-2", "domain": ["keyword:pornhub"], "outboundTag": "DIRECT"}]

    assert own[0]["domain"] == ["full:pornhub.com"]
    assert _quiet(rules.conflicting_rules(misses, ["in"], None, own)) == []
    assert _named(rules.conflicting_rules(hits, ["in"], None, own)) == ["op-2"]


def test_a_domain_rule_that_cannot_be_resolved_against_a_category_list_advises():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=["adult"],
        allow_list=[],
        block_list=[],
        strict_mode=False,
    )
    existing = [{"type": "field", "ruleTag": "op-1", "domain": ["domain:unrelated.example"], "outboundTag": "DIRECT"}]

    result = rules.conflicting_rules(existing, ["in"], None, own)

    assert result.clashes == []
    assert result.advisories == [
        "op-1 could send a hostname matching domain:unrelated.example against geosite:category-porn to DIRECT"
    ]


def test_an_unidentified_live_rule_never_inherits_another_rules_restriction():
    config = {
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}, {"tag": "BLOCK", "protocol": "blackhole"}],
        "routing": {
            "rules": [
                {"type": "field", "ruleTag": "op-narrow", "domain": ["domain:wikipedia.org"], "outboundTag": "DIRECT"}
            ]
        },
    }
    own = _block_only(1, "in", "pornhub.com")
    accounted = [{"ruleTag": "op-narrow", "outboundTag": "DIRECT"}]
    surplus = accounted + [{"ruleTag": "injected-catch-all", "outboundTag": "DIRECT"}]

    assert _quiet(rules.conflicting_rules(accounted, ["in"], config, own)) == []
    assert rules.conflicting_rules(surplus, ["in"], config, own).clashes == [
        "an unidentified rule sends every connection to DIRECT"
    ]


def test_an_untagged_live_rule_is_still_matched_against_the_saved_rules_that_account_for_it():
    own = _block_only(1, "quiet-inbound", "instagram.com")
    stubs = [{"ruleTag": "", "outboundTag": str(rule.get("outboundTag") or "")} for rule in PRODUCTION_RULES]

    result = rules.conflicting_rules(stubs, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert not any("unidentified" in clash for clash in _quiet(result))
    assert _named(result) == ["an untagged rule"]


def test_a_surplus_untagged_live_rule_is_reported_even_on_the_production_core():
    own = _block_only(1, "quiet-inbound", "instagram.com")
    stubs = [{"ruleTag": "", "outboundTag": str(rule.get("outboundTag") or "")} for rule in PRODUCTION_RULES]
    stubs.append({"ruleTag": "", "outboundTag": "gemini-usa"})

    result = rules.conflicting_rules(stubs, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert "an unidentified rule sends every connection to gemini-usa" in result.clashes


ALLOW_SWALLOW_CORE = {
    "outbounds": [
        {"tag": "DIRECT", "protocol": "freedom"},
        {"tag": "BLOCK", "protocol": "blackhole"},
        {"tag": "PROXY", "protocol": "vless"},
    ]
}


@pytest.mark.parametrize("outbound", ["BLOCK", "PROXY"])
def test_a_rule_that_swallows_an_allow_listed_domain_is_reported(outbound: str):
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["allowed.example"],
        block_list=["pornhub.com"],
        strict_mode=False,
    )
    existing = [
        {
            "type": "field",
            "ruleTag": "op-1",
            "domain": ["domain:allowed.example"],
            "inboundTag": ["in"],
            "outboundTag": outbound,
        }
    ]

    result = rules.conflicting_rules(existing, ["in"], ALLOW_SWALLOW_CORE, own)

    assert _named(result) == ["op-1"]
    assert "domain:allowed.example" in result.clashes[0]


def test_a_rule_sending_an_allow_listed_domain_direct_matches_what_the_allow_was_for():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["allowed.example"],
        block_list=["pornhub.com"],
        strict_mode=False,
    )
    existing = [
        {
            "type": "field",
            "ruleTag": "op-1",
            "domain": ["domain:allowed.example"],
            "inboundTag": ["in"],
            "outboundTag": "DIRECT",
        }
    ]

    assert _quiet(rules.conflicting_rules(existing, ["in"], ALLOW_SWALLOW_CORE, own)) == []


def test_the_production_core_lets_an_unrelated_filter_through_with_an_advisory():
    own = _block_only(1, "quiet-inbound", "adult.example")

    result = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert result.clashes == []
    assert result.advisories == ["an untagged rule could send a hostname matching keyword:instagram to DIRECT"]


def test_the_production_core_still_refuses_a_category_that_really_contains_the_keyword():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["quiet-inbound"],
        categories=["social_network"],
        allow_list=[],
        block_list=[],
        strict_mode=False,
    )

    result = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert result.clashes == ["an untagged rule sends keyword:instagram to DIRECT"]
    assert result.advisories == []


def test_the_production_core_advises_rather_than_refuses_a_category_without_the_keyword():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["quiet-inbound"],
        categories=["adult"],
        allow_list=[],
        block_list=[],
        strict_mode=False,
    )

    result = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert result.clashes == []
    assert result.advisories == [
        "an untagged rule could send a hostname matching keyword:instagram against geosite:category-porn to DIRECT"
    ]


def _category_profile(category: str, inbound: str = "in", strict: bool = False) -> list[dict]:
    return rules.build_rules(
        assignment_id=1,
        inbound_tags=[inbound],
        categories=[category],
        allow_list=[],
        block_list=[],
        strict_mode=strict,
    )


def _operator_rule(matcher: str, outbound: str = "DIRECT") -> list[dict]:
    return [{"type": "field", "ruleTag": "op-1", "domain": [matcher], "outboundTag": outbound}]


@pytest.mark.parametrize("matcher", ["geosite:google-gemini", "ext:other.dat:list", "regexp:^ads"])
def test_their_matchers_the_panel_cannot_expand_advise_instead_of_refusing(matcher: str):
    result = rules.conflicting_rules(_operator_rule(matcher), ["in"], None, _category_profile("adult"))

    assert result.clashes == []
    assert result.advisories == [
        f"op-1 could send a hostname matching {matcher} against geosite:category-porn to DIRECT"
    ]


def test_a_regexp_that_cannot_touch_the_block_list_is_not_a_blocking_clash():
    own = _block_only(1, "in", "blocked.example")

    result = rules.conflicting_rules(_operator_rule(r"regexp:^safe\.example$"), ["in"], None, own)

    assert result.clashes == []
    assert result.advisories == [r"op-1 could send a hostname matching regexp:^safe\.example$ to DIRECT"]


def test_a_regexp_that_reads_like_the_blocked_domain_is_still_only_an_advisory():
    own = _block_only(1, "in", "blocked.example")

    result = rules.conflicting_rules(_operator_rule(r"regexp:^blocked\.example$"), ["in"], None, own)

    assert result.clashes == []
    assert result.advisories == [r"op-1 could send a hostname matching regexp:^blocked\.example$ to DIRECT"]


def test_a_regexp_is_still_refused_where_strict_mode_proves_the_bypass():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=[],
        block_list=["blocked.example"],
        strict_mode=True,
    )

    result = rules.conflicting_rules(_operator_rule(r"regexp:^safe\.example$"), ["in"], None, own)

    assert _named(result) == ["op-1"]
    assert result.advisories == []


def test_the_same_geosite_on_both_sides_is_still_refused():
    result = rules.conflicting_rules(_operator_rule("geosite:category-porn"), ["in"], None, _category_profile("adult"))

    assert result.clashes == ["op-1 sends geosite:category-porn to DIRECT"]
    assert result.advisories == []


def test_a_geosite_the_filter_never_selected_only_advises():
    result = rules.conflicting_rules(_operator_rule("geosite:google-gemini"), ["in"], None, _category_profile("adult"))

    assert result.clashes == []
    assert result.advisories == [
        "op-1 could send a hostname matching geosite:google-gemini against geosite:category-porn to DIRECT"
    ]


def test_the_same_external_list_on_both_sides_is_still_refused():
    own = _category_profile("pglist-1")

    result = rules.conflicting_rules(_operator_rule("ext:pgfilter.dat:pglist-1"), ["in"], None, own)

    assert own[0]["domain"] == ["ext:pgfilter.dat:pglist-1"]
    assert result.clashes == ["op-1 sends ext:pgfilter.dat:pglist-1 to DIRECT"]
    assert result.advisories == []


def test_an_external_list_from_another_file_only_advises():
    own = _category_profile("pglist-1")

    result = rules.conflicting_rules(_operator_rule("ext:other.dat:pglist-1"), ["in"], None, own)

    assert result.clashes == []
    assert result.advisories == [
        "op-1 could send a hostname matching ext:other.dat:pglist-1 against ext:pgfilter.dat:pglist-1 to DIRECT"
    ]


@pytest.mark.parametrize("matcher", ["geosite:google-gemini", "ext:other.dat:list", r"regexp:^safe\.example$"])
def test_an_unexpandable_matcher_against_a_concrete_filter_names_only_itself(matcher: str):
    result = rules.conflicting_rules(_operator_rule(matcher), ["in"], None, _block_only(1, "in", "blocked.example"))

    assert result.clashes == []
    assert result.advisories == [f"op-1 could send a hostname matching {matcher} to DIRECT"]


@pytest.mark.parametrize("matcher", ["geosite:category-porn", "ext:pgfilter.dat:pglist-1", r"regexp:^allowed\."])
def test_an_unexpandable_matcher_over_an_allow_list_advises_rather_than_refusing(matcher: str):
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["allowed.example"],
        block_list=[],
        strict_mode=False,
    )

    result = rules.conflicting_rules(_operator_rule(matcher, "BLOCK"), ["in"], CATCH_ALL_CORE, own)

    assert result.clashes == []
    assert result.advisories == [
        f"op-1 sends {matcher} to BLOCK, blocking part of a destination this profile's allow list permits"
    ]


@pytest.mark.parametrize(
    "matcher",
    ["domain:blocked.example", "full:blocked.example", "domain:example", "keyword:blocked", "keyword:ocked.exam"],
)
def test_a_concrete_matcher_that_really_reaches_the_block_list_is_still_refused(matcher: str):
    result = rules.conflicting_rules(_operator_rule(matcher), ["in"], None, _block_only(1, "in", "blocked.example"))

    assert result.clashes == [f"op-1 sends {matcher} to DIRECT"]
    assert result.advisories == []


def test_a_provable_hit_later_in_the_same_rule_outranks_an_earlier_advisory():
    existing = [
        {
            "type": "field",
            "ruleTag": "op-1",
            "domain": [r"regexp:^safe\.example$", "domain:blocked.example"],
            "outboundTag": "DIRECT",
        }
    ]

    result = rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "blocked.example"))

    assert result.clashes == ["op-1 sends domain:blocked.example to DIRECT"]
    assert result.advisories == []


GEMINI_INBOUND = "vless80"

PRODUCTION_PROFILES = {
    "block only": {"block_list": ["adult.example"]},
    "allow-only non-strict": {"allow_list": ["allowed.example"]},
    "strict": {"block_list": ["adult.example"], "strict_mode": True},
    "allow+block": {"allow_list": ["allowed.example"], "block_list": ["pornhub.com"]},
    "social category": {"categories": ["social_network"]},
    "adult category": {"categories": ["adult"]},
}


def _production_verdict(shape: str, inbound: str):
    profile = {
        "assignment_id": 1,
        "inbound_tags": [inbound],
        "categories": [],
        "allow_list": [],
        "block_list": [],
        "strict_mode": False,
    }
    profile.update(PRODUCTION_PROFILES[shape])

    return rules.conflicting_rules(PRODUCTION_RULES, [inbound], PRODUCTION_CORE, rules.build_rules(**profile))


@pytest.mark.parametrize("shape", [name for name in PRODUCTION_PROFILES if name != "strict"])
def test_the_production_gemini_inbound_refuses_no_profile_it_cannot_prove(shape: str):
    result = _production_verdict(shape, GEMINI_INBOUND)

    assert [clash for clash in result.clashes if "gemini" in clash] == []
    assert [advisory for advisory in result.advisories if "geosite:google-gemini" in advisory] != []


def test_the_production_gemini_rule_still_refuses_a_strict_profile_on_its_own_inbound():
    result = _production_verdict("strict", GEMINI_INBOUND)

    assert result.clashes == [
        "an untagged rule sends keyword:instagram to DIRECT",
        "an untagged rule sends traffic for 100.64.0.0/10 to DIRECT",
        "an untagged rule sends geosite:google-gemini to gemini-usa",
    ]
    assert result.advisories == []


@pytest.mark.parametrize("shape", [name for name in PRODUCTION_PROFILES if name != "strict"])
def test_the_gemini_inbound_carries_the_same_refusals_as_a_quiet_one(shape: str):
    on_gemini = _production_verdict(shape, GEMINI_INBOUND)
    elsewhere = _production_verdict(shape, "quiet-inbound")

    assert on_gemini.clashes == elsewhere.clashes


def test_the_only_production_shape_that_refuses_anything_is_strict_mode():
    refusing = {shape for shape in PRODUCTION_PROFILES if _production_verdict(shape, GEMINI_INBOUND).clashes}

    assert refusing == {"strict", "social category"}
    assert _production_verdict("social category", GEMINI_INBOUND).clashes == [
        "an untagged rule sends keyword:instagram to DIRECT"
    ]


def test_an_advisory_names_more_than_one_category_without_listing_them_all():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=["social_network"],
        allow_list=[],
        block_list=[],
        strict_mode=False,
    )
    existing = [{"type": "field", "ruleTag": "op-1", "domain": ["keyword:zzqqxx"], "outboundTag": "DIRECT"}]

    result = rules.conflicting_rules(existing, ["in"], None, own)

    assert result.clashes == []
    assert "keyword:zzqqxx against " in result.advisories[0]
    assert " more to DIRECT" in result.advisories[0]


def test_the_production_core_under_strict_mode_reports_both_direct_rules():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["quiet-inbound"],
        categories=[],
        allow_list=[],
        block_list=["adult.example"],
        strict_mode=True,
    )

    result = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert result.clashes == [
        "an untagged rule sends keyword:instagram to DIRECT",
        "an untagged rule sends traffic for 100.64.0.0/10 to DIRECT",
    ]
    assert result.advisories == []


def test_the_result_names_its_two_lists_and_still_unpacks_as_a_pair():
    own = _block_only(1, "in", "pornhub.com")
    existing = [{"type": "field", "ruleTag": "op-1", "domain": ["keyword:ads"], "outboundTag": "DIRECT"}]

    result = rules.conflicting_rules(existing, ["in"], None, own)
    clashes, advisories = result

    assert clashes == result.clashes == []
    assert advisories == result.advisories
    assert len(advisories) == 1


def test_the_advisory_note_says_the_filter_was_applied():
    assert "do not stop the filter" in rules.ADVISORY_NOTE
    assert "scope or narrow" in rules.ADVISORY_NOTE


API_HANDLER_CORE = {"outbounds": [{"tag": "BLOCK", "protocol": "blackhole"}]}
API_OUTBOUND_CORE = {"outbounds": [{"tag": "BLOCK", "protocol": "blackhole"}, {"tag": "api", "protocol": "freedom"}]}
API_UPPER_CORE = {"outbounds": [{"tag": "BLOCK", "protocol": "blackhole"}, {"tag": "API", "protocol": "freedom"}]}


def test_the_implicit_api_handler_is_exempt_when_no_core_outbound_claims_that_name():
    existing = [{"type": "field", "ruleTag": "op-api", "inboundTag": ["in"], "outboundTag": "api"}]

    assert _quiet(rules.conflicting_rules(existing, ["in"], API_HANDLER_CORE, _block_only(1, "in", "x.example"))) == []


def test_a_core_outbound_actually_named_api_is_checked_like_any_other():
    existing = [{"type": "field", "ruleTag": "op-api", "inboundTag": ["in"], "outboundTag": "api"}]

    result = rules.conflicting_rules(existing, ["in"], API_OUTBOUND_CORE, _block_only(1, "in", "x.example"))

    assert _named(result) == ["op-api"]


@pytest.mark.parametrize("outbound", ["api", "API"])
def test_neither_api_spelling_is_exempt_once_the_core_declares_one_of_them(outbound: str):
    existing = [{"type": "field", "ruleTag": "op-api", "inboundTag": ["in"], "outboundTag": outbound}]

    result = rules.conflicting_rules(existing, ["in"], API_UPPER_CORE, _block_only(1, "in", "x.example"))

    assert _named(result) == ["op-api"]


def test_a_bare_domain_entry_is_read_as_the_substring_matcher_xray_makes_of_it():
    assert rules._split_matcher("speedtest.net") == ("keyword", "speedtest.net")
    assert rules._split_matcher("keyword:speedtest.net") == ("keyword", "speedtest.net")
    assert rules._split_matcher("domain:speedtest.net") == ("domain", "speedtest.net")


def test_a_bare_domain_entry_against_an_unrelated_filter_advises_rather_than_refusing():
    existing = [{"type": "field", "ruleTag": "op-st", "domain": ["speedtest.net"], "outboundTag": "DIRECT"}]

    result = rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "pornhub.com"))

    assert result.clashes == []
    assert result.advisories == ["op-st could send a hostname matching speedtest.net to DIRECT"]


def test_a_bare_domain_entry_that_really_matches_the_filter_still_refuses():
    existing = [{"type": "field", "ruleTag": "op-st", "domain": ["pornhub"], "outboundTag": "DIRECT"}]

    result = rules.conflicting_rules(existing, ["in"], None, _block_only(1, "in", "pornhub.com"))

    assert _named(result) == ["op-st"]
    assert result.advisories == []


ALLOW_GUARD_CORE = {"outbounds": [{"tag": "BLOCK", "protocol": "blackhole"}, {"tag": "DIRECT", "protocol": "freedom"}]}

SWALLOWS_THE_ALLOW_LIST = [
    ("catch-all", {}),
    ("public addresses", {"ip": ["192.88.99.0/24"]}),
    ("client subnet", {"source": ["10.0.0.0/8"]}),
]

DEGRADES_THE_ALLOW_LIST = [
    ("one transport", {"network": "tcp"}),
    ("both transports named", {"network": "tcp,udp"}),
    ("one port", {"port": "443"}),
    ("a port the site does not use", {"port": "25"}),
    ("quic only", {"network": "udp", "port": "443"}),
    ("a public range on one transport", {"ip": ["geoip:facebook"], "port": "443", "network": "udp"}),
]

CANNOT_BREAK_THE_ALLOW_LIST = [
    ("non-public addresses", {"ip": ["10.0.0.0/8"]}),
]


def _allowing_profile() -> list[dict]:
    return rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["allowed.example"],
        block_list=["pornhub.com"],
        strict_mode=False,
    )


@pytest.mark.parametrize(("name", "predicate"), SWALLOWS_THE_ALLOW_LIST)
def test_a_blackhole_rule_that_swallows_allow_listed_traffic_is_reported(name: str, predicate: dict):
    existing = [{"type": "field", "ruleTag": "op-1", **predicate, "inboundTag": ["in"], "outboundTag": "BLOCK"}]

    result = rules.conflicting_rules(existing, ["in"], ALLOW_GUARD_CORE, _allowing_profile())

    assert _named(result) == ["op-1"]
    assert result.advisories == []
    assert result.clashes[0].endswith("blocking a destination this profile's allow list permits")


@pytest.mark.parametrize(("name", "predicate"), DEGRADES_THE_ALLOW_LIST)
def test_a_rule_that_blocks_part_of_an_allow_listed_destination_advises_without_refusing(name: str, predicate: dict):
    existing = [{"type": "field", "ruleTag": "op-1", **predicate, "inboundTag": ["in"], "outboundTag": "BLOCK"}]

    result = rules.conflicting_rules(existing, ["in"], ALLOW_GUARD_CORE, _allowing_profile())

    assert result.clashes == []
    assert len(result.advisories) == 1
    assert result.advisories[0].endswith("blocking part of a destination this profile's allow list permits")


@pytest.mark.parametrize(("name", "predicate"), DEGRADES_THE_ALLOW_LIST)
def test_a_partial_rule_is_silent_when_the_profile_has_no_allow_list(name: str, predicate: dict):
    existing = [{"type": "field", "ruleTag": "op-1", **predicate, "inboundTag": ["in"], "outboundTag": "BLOCK"}]

    assert (
        _quiet(rules.conflicting_rules(existing, ["in"], ALLOW_GUARD_CORE, _block_only(1, "in", "pornhub.com"))) == []
    )


@pytest.mark.parametrize(("name", "predicate"), SWALLOWS_THE_ALLOW_LIST)
def test_the_same_rule_is_silent_when_the_profile_has_no_allow_list(name: str, predicate: dict):
    existing = [{"type": "field", "ruleTag": "op-1", **predicate, "inboundTag": ["in"], "outboundTag": "BLOCK"}]

    assert (
        _quiet(rules.conflicting_rules(existing, ["in"], ALLOW_GUARD_CORE, _block_only(1, "in", "pornhub.com"))) == []
    )


@pytest.mark.parametrize(("name", "predicate"), CANNOT_BREAK_THE_ALLOW_LIST)
def test_a_blackhole_rule_that_cannot_reach_the_allowed_site_stays_silent(name: str, predicate: dict):
    existing = [{"type": "field", "ruleTag": "op-1", **predicate, "inboundTag": ["in"], "outboundTag": "BLOCK"}]

    assert _quiet(rules.conflicting_rules(existing, ["in"], ALLOW_GUARD_CORE, _allowing_profile())) == []
    assert _quiet(rules.conflicting_rules(existing, ["in"], ALLOW_GUARD_CORE, _block_only(1, "in", "x.example"))) == []


def test_the_production_blocking_rules_advise_but_never_refuse_an_allow_list():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["quiet-inbound"],
        categories=[],
        allow_list=["allowed.example"],
        block_list=["adult.example"],
        strict_mode=False,
    )

    result = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert result.clashes == []
    assert any("keyword:instagram" in advisory for advisory in result.advisories)
    assert all("allow list permits" in advisory or "keyword:instagram" in advisory for advisory in result.advisories)


def test_a_rule_that_only_removes_quic_from_an_allow_listed_domain_advises():
    existing = [
        {
            "type": "field",
            "ruleTag": "op-1",
            "domain": ["domain:allowed.example"],
            "network": "udp",
            "inboundTag": ["in"],
            "outboundTag": "BLOCK",
        }
    ]

    result = rules.conflicting_rules(existing, ["in"], ALLOW_GUARD_CORE, _allowing_profile())

    assert result.clashes == []
    assert result.advisories == [
        "op-1 sends domain:allowed.example to BLOCK, blocking part of a destination this profile's allow list permits"
    ]


NETWORK_REACHES_TCP = ["tcp", "tcp,udp", "tcp, udp", ["tcp", "udp"]]
NETWORK_MISSES_TCP = ["udp", ["udp"], "udp,quic"]


@pytest.mark.parametrize("network", [*NETWORK_REACHES_TCP, *NETWORK_MISSES_TCP])
def test_every_network_spelling_is_parsed_and_advised_against_an_allow_list(network):
    existing = [{"type": "field", "ruleTag": "op-1", "network": network, "inboundTag": ["in"], "outboundTag": "BLOCK"}]

    result = rules.conflicting_rules(existing, ["in"], ALLOW_GUARD_CORE, _allowing_profile())

    assert result.clashes == []
    assert len(result.advisories) == 1


def test_a_missing_network_key_means_every_transport_and_refuses_outright():
    existing = [{"type": "field", "ruleTag": "op-1", "inboundTag": ["in"], "outboundTag": "BLOCK"}]

    assert _named(rules.conflicting_rules(existing, ["in"], ALLOW_GUARD_CORE, _allowing_profile())) == ["op-1"]


def _two_endpoint_filters() -> list[dict]:
    return rules.build_rules(1, ["A"], [], [], ["a.example"], False) + rules.build_rules(
        2, ["B"], [], [], ["b.example"], False
    )


def test_a_rule_scoped_to_one_endpoint_is_not_judged_by_another_endpoints_profile():
    existing = [
        {
            "type": "field",
            "ruleTag": "op-1",
            "domain": ["domain:b.example"],
            "inboundTag": ["A"],
            "outboundTag": "DIRECT",
        }
    ]

    assert _quiet(rules.conflicting_rules(existing, ["A", "B"], ALLOW_GUARD_CORE, _two_endpoint_filters())) == []


@pytest.mark.parametrize(
    ("matcher", "bound"),
    [("domain:b.example", ["B"]), ("domain:a.example", ["A"])],
)
def test_a_rule_that_really_meets_its_own_endpoints_profile_is_still_reported(matcher: str, bound: list):
    existing = [{"type": "field", "ruleTag": "op-1", "domain": [matcher], "inboundTag": bound, "outboundTag": "DIRECT"}]

    assert _named(rules.conflicting_rules(existing, ["A", "B"], ALLOW_GUARD_CORE, _two_endpoint_filters())) == ["op-1"]


def test_an_unscoped_existing_rule_still_meets_every_endpoints_profile():
    existing = [{"type": "field", "ruleTag": "op-1", "domain": ["domain:b.example"], "outboundTag": "DIRECT"}]

    assert _named(rules.conflicting_rules(existing, ["A", "B"], ALLOW_GUARD_CORE, _two_endpoint_filters())) == ["op-1"]


def test_an_unscoped_filter_rule_still_meets_every_existing_rule():
    own = rules.build_rules(3, [], [], [], ["a.example"], False)
    existing = [
        {
            "type": "field",
            "ruleTag": "op-1",
            "domain": ["domain:a.example"],
            "inboundTag": ["Z"],
            "outboundTag": "DIRECT",
        }
    ]

    assert _named(rules.conflicting_rules(existing, [], ALLOW_GUARD_CORE, own)) == ["op-1"]


def test_strict_mode_on_one_endpoint_does_not_condemn_a_rule_on_another():
    strict_a = rules.build_rules(1, ["A"], [], [], ["a.example"], True)
    relaxed_b = rules.build_rules(2, ["B"], [], [], ["b.example"], False)
    on_b = [{"type": "field", "ruleTag": "op-1", "ip": ["10.0.0.0/8"], "inboundTag": ["B"], "outboundTag": "DIRECT"}]
    on_a = [{"type": "field", "ruleTag": "op-2", "ip": ["10.0.0.0/8"], "inboundTag": ["A"], "outboundTag": "DIRECT"}]

    both = strict_a + relaxed_b

    assert _quiet(rules.conflicting_rules(on_b, ["A", "B"], ALLOW_GUARD_CORE, both)) == []
    assert _named(rules.conflicting_rules(on_a, ["A", "B"], ALLOW_GUARD_CORE, both)) == ["op-2"]


CATCH_ALL_CORE = {
    "outbounds": [
        {"tag": "BLOCK", "protocol": "blackhole"},
        {"tag": "DIRECT", "protocol": "freedom"},
        {"tag": "PROXY", "protocol": "vless"},
    ],
    "routing": {"rules": []},
}


def _allow_only_profile(strict: bool = False) -> list[dict]:
    return rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["allowed.example"],
        block_list=[],
        strict_mode=strict,
    )


def _catch_all(tag: str, outbound: str) -> list[dict]:
    return [{"type": "field", "ruleTag": tag, "inboundTag": ["in"], "outboundTag": outbound}]


def test_an_allow_only_profile_has_nothing_for_a_direct_catch_all_to_bypass():
    assert (
        _quiet(rules.conflicting_rules(_catch_all("op-1", "DIRECT"), ["in"], CATCH_ALL_CORE, _allow_only_profile()))
        == []
    )


@pytest.mark.parametrize("outbound", ["BLOCK", "PROXY"])
def test_an_allow_only_profile_still_reports_a_catch_all_that_swallows_its_allow_list(outbound: str):
    result = rules.conflicting_rules(_catch_all("op-1", outbound), ["in"], CATCH_ALL_CORE, _allow_only_profile())

    assert _named(result) == ["op-1"]
    assert result.clashes[0].endswith("blocking a destination this profile's allow list permits")


def test_a_profile_that_blocks_something_still_reports_a_direct_catch_all():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["allowed.example"],
        block_list=["pornhub.com"],
        strict_mode=False,
    )

    assert _named(rules.conflicting_rules(_catch_all("op-1", "DIRECT"), ["in"], CATCH_ALL_CORE, own)) == ["op-1"]


def test_an_allow_only_profile_in_strict_mode_still_reports_a_direct_catch_all():
    assert _named(
        rules.conflicting_rules(_catch_all("op-1", "DIRECT"), ["in"], CATCH_ALL_CORE, _allow_only_profile(strict=True))
    ) == ["op-1"]


def test_an_unidentified_blackhole_stub_is_reported_when_there_is_an_allow_list_to_protect():
    stubs = [{"ruleTag": "ghost", "outboundTag": "BLOCK"}]

    result = rules.conflicting_rules(stubs, ["in"], CATCH_ALL_CORE, _allow_only_profile())

    assert result.clashes == [
        "an unidentified rule sends every connection to BLOCK, blocking a destination this profile's allow list permits"
    ]


def test_an_unidentified_blackhole_stub_is_silent_when_there_is_no_allow_list():
    stubs = [{"ruleTag": "ghost", "outboundTag": "BLOCK"}]

    assert _quiet(rules.conflicting_rules(stubs, ["in"], CATCH_ALL_CORE, _block_only(1, "in", "pornhub.com"))) == []


def test_an_unidentified_stub_on_a_non_harmless_outbound_is_reported_either_way():
    stubs = [{"ruleTag": "ghost", "outboundTag": "PROXY"}]

    blocking = rules.conflicting_rules(stubs, ["in"], CATCH_ALL_CORE, _block_only(1, "in", "pornhub.com"))
    allow_only = rules.conflicting_rules(stubs, ["in"], CATCH_ALL_CORE, _allow_only_profile())

    assert blocking.clashes == ["an unidentified rule sends every connection to PROXY"]
    assert allow_only.clashes == [
        "an unidentified rule sends every connection to PROXY, blocking a destination this profile's allow list permits"
    ]


def test_the_production_core_never_refuses_an_allow_only_profile():
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["quiet-inbound"],
        categories=[],
        allow_list=["allowed.example"],
        block_list=[],
        strict_mode=False,
    )

    result = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert result.clashes == []
    assert len(result.advisories) == 2
    assert all(
        advisory.endswith("blocking part of a destination this profile's allow list permits")
        for advisory in result.advisories
    )


def test_a_balancer_rule_names_its_balancer_rather_than_an_empty_destination():
    existing = [
        {
            "type": "field",
            "ruleTag": "op-bal",
            "domain": ["domain:pornhub.com"],
            "inboundTag": ["in"],
            "balancerTag": "round-robin",
        }
    ]

    result = rules.conflicting_rules(existing, ["in"], CATCH_ALL_CORE, _block_only(1, "in", "pornhub.com"))

    assert result.clashes == ["op-bal sends domain:pornhub.com to balancer round-robin"]


def test_a_rule_with_no_destination_at_all_names_the_default_outbound():
    existing = [{"type": "field", "ruleTag": "op-none", "domain": ["domain:pornhub.com"], "inboundTag": ["in"]}]

    result = rules.conflicting_rules(existing, ["in"], CATCH_ALL_CORE, _block_only(1, "in", "pornhub.com"))

    assert result.clashes == ["op-none sends domain:pornhub.com to the default outbound"]


REWRITING_CORE = {
    "outbounds": [
        {"tag": "BLOCK", "protocol": "blackhole"},
        {"tag": "detour", "protocol": "freedom", "settings": {"redirect": "10.9.9.9:8080"}},
    ]
}

MIXED_FREEDOM_CORE = {
    "outbounds": [
        {"tag": "BLOCK", "protocol": "blackhole"},
        {"tag": "detour", "protocol": "freedom", "settings": {"redirect": "10.9.9.9:8080"}},
        {"tag": "plain", "protocol": "freedom"},
    ]
}


def test_a_freedom_outbound_that_rewrites_the_destination_is_never_resolved_as_direct():
    assert rules.resolve_outbound_tags(REWRITING_CORE) == {"block": "BLOCK"}


def test_a_clean_freedom_outbound_is_preferred_over_an_alphabetically_earlier_rewriting_one():
    assert rules.resolve_outbound_tags(MIXED_FREEDOM_CORE) == {"block": "BLOCK", "direct": "plain"}


def test_a_rewriting_outbound_is_not_exempt_from_the_allow_list_check():
    existing = [
        {
            "type": "field",
            "ruleTag": "op-1",
            "domain": ["domain:allowed.example"],
            "inboundTag": ["in"],
            "outboundTag": "detour",
        }
    ]

    result = rules.conflicting_rules(existing, ["in"], REWRITING_CORE, _allowing_profile())

    assert _named(result) == ["op-1"]
    assert result.clashes[0].endswith("blocking a destination this profile's allow list permits")


def test_a_clean_freedom_outbound_is_still_exempt_from_the_allow_list_check():
    existing = [
        {
            "type": "field",
            "ruleTag": "op-1",
            "domain": ["domain:allowed.example"],
            "inboundTag": ["in"],
            "outboundTag": "plain",
        }
    ]

    assert _quiet(rules.conflicting_rules(existing, ["in"], MIXED_FREEDOM_CORE, _allowing_profile())) == []


@pytest.mark.parametrize("redirect", ["192.88.99.1:443", "192.88.99.1:0", ":8080", "[::1]:443", "nonsense"])
def test_a_redirect_that_changes_address_or_port_counts_as_a_rewrite(redirect: str):
    assert rules.rewrites_destination({"protocol": "freedom", "settings": {"redirect": redirect}}) is True


@pytest.mark.parametrize("redirect", [":0", "", "   "])
def test_a_redirect_that_changes_nothing_is_not_a_rewrite(redirect: str):
    assert rules.rewrites_destination({"protocol": "freedom", "settings": {"redirect": redirect}}) is False


def test_an_outbound_with_no_settings_is_not_a_rewrite():
    assert rules.rewrites_destination({"tag": "plain", "protocol": "freedom"}) is False
    assert rules.rewrites_destination({"tag": "plain", "protocol": "freedom", "settings": None}) is False


def test_a_blackhole_response_setting_never_makes_it_forward():
    for response in ({"type": "none"}, {"type": "http"}, None):
        entry = {"tag": "BLOCK", "protocol": "blackhole", "settings": {"response": response}}
        assert rules.rewrites_destination(entry) is False
        assert rules.blocking_outbound_tags({"outbounds": [entry]}) == {"BLOCK"}


def _strategy_core(strategy: str | None) -> dict:
    routing: dict = {"rules": []}
    if strategy is not None:
        routing["domainStrategy"] = strategy
    return {
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}, {"tag": "BLOCK", "protocol": "blackhole"}],
        "routing": routing,
    }


@pytest.mark.parametrize("strategy", [None, "AsIs", "asis", "IPIfNonMatch", "ipifnonmatch", "nonsense"])
def test_a_private_address_rule_is_exempt_while_routing_matches_on_the_hostname(strategy):
    existing = [
        {"type": "field", "ruleTag": "op-1", "ip": ["geoip:private"], "inboundTag": ["in"], "outboundTag": "DIRECT"}
    ]

    assert rules.resolves_before_matching(_strategy_core(strategy)) is False
    assert (
        _quiet(rules.conflicting_rules(existing, ["in"], _strategy_core(strategy), _block_only(1, "in", "x.example")))
        == []
    )


@pytest.mark.parametrize("strategy", ["IPOnDemand", "ipondemand", " IPOnDemand "])
def test_a_private_address_rule_is_reported_when_the_core_resolves_before_matching(strategy):
    existing = [
        {"type": "field", "ruleTag": "op-1", "ip": ["geoip:private"], "inboundTag": ["in"], "outboundTag": "DIRECT"}
    ]

    assert rules.resolves_before_matching(_strategy_core(strategy)) is True
    assert _named(
        rules.conflicting_rules(existing, ["in"], _strategy_core(strategy), _block_only(1, "in", "x.example"))
    ) == ["op-1"]


def test_the_allow_side_private_exemption_follows_the_same_strategy():
    existing = [
        {"type": "field", "ruleTag": "op-1", "ip": ["10.0.0.0/8"], "inboundTag": ["in"], "outboundTag": "BLOCK"}
    ]
    own = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=["allowed.example"],
        block_list=["pornhub.com"],
        strict_mode=False,
    )

    assert _quiet(rules.conflicting_rules(existing, ["in"], _strategy_core("IPIfNonMatch"), own)) == []
    assert _named(rules.conflicting_rules(existing, ["in"], _strategy_core("IPOnDemand"), own)) == ["op-1"]


def test_the_production_cores_strategy_keeps_the_cgnat_rule_silent():
    assert rules.resolves_before_matching(PRODUCTION_CORE) is False

    own = _block_only(1, "quiet-inbound", "adult.example")
    result = rules.conflicting_rules(PRODUCTION_RULES, ["quiet-inbound"], PRODUCTION_CORE, own)

    assert not any("100.64.0.0/10" in message for message in _quiet(result))


def test_an_unidentified_stub_is_measured_against_every_saved_rule_sharing_its_outbound():
    core = {
        "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}, {"tag": "BLOCK", "protocol": "blackhole"}],
        "routing": {
            "rules": [
                {"type": "field", "ruleTag": "", "domain": ["domain:pornhub.com"], "outboundTag": "DIRECT"},
                {"type": "field", "ruleTag": "", "ip": ["100.64.0.0/10"], "outboundTag": "DIRECT"},
            ]
        },
    }
    stubs = [{"ruleTag": "", "outboundTag": "DIRECT"}, {"ruleTag": "", "outboundTag": "DIRECT"}]

    result = rules.conflicting_rules(stubs, ["in"], core, _block_only(1, "in", "pornhub.com"))

    assert result.clashes == ["an untagged rule sends domain:pornhub.com to DIRECT"]


STRICT_CAPABLE_STRATEGIES = ["IPIfNonMatch", "ipifnonmatch", "IPOnDemand", "ipondemand", " IPOnDemand "]
STRICT_DEAD_STRATEGIES = [None, "AsIs", "asis", "", "   ", "nonsense"]


@pytest.mark.parametrize("strategy", STRICT_CAPABLE_STRATEGIES)
def test_strict_mode_can_fire_where_routing_resolves_a_hostname_at_some_point(strategy):
    assert rules.strict_mode_effective(_strategy_core(strategy)) is True


@pytest.mark.parametrize("strategy", STRICT_DEAD_STRATEGIES)
def test_strict_mode_cannot_fire_where_routing_never_resolves_a_hostname(strategy):
    assert rules.strict_mode_effective(_strategy_core(strategy)) is False


def test_an_absent_strategy_key_is_treated_as_the_asis_default_xray_itself_applies():
    assert rules.strict_mode_effective({"routing": {"rules": []}}) is False
    assert rules.strict_mode_effective({"outbounds": []}) is False
    assert rules.strict_mode_effective({}) is False
    assert rules.strict_mode_effective(None) is False


def test_the_two_strategy_predicates_are_not_inverses_of_each_other():
    resolves_first = _strategy_core("IPOnDemand")
    resolves_on_retry = _strategy_core("IPIfNonMatch")
    never_resolves = _strategy_core("AsIs")

    assert rules.resolves_before_matching(resolves_first) is True
    assert rules.strict_mode_effective(resolves_first) is True

    assert rules.resolves_before_matching(resolves_on_retry) is False
    assert rules.strict_mode_effective(resolves_on_retry) is True

    assert rules.resolves_before_matching(never_resolves) is False
    assert rules.strict_mode_effective(never_resolves) is False


def test_the_production_core_can_enforce_strict_mode_but_a_default_core_cannot():
    assert rules.strict_mode_effective(PRODUCTION_CORE) is True
    assert rules.strict_mode_effective({"outbounds": PRODUCTION_CORE["outbounds"], "routing": {"rules": []}}) is False


def test_the_strict_rule_this_builder_emits_is_the_one_the_predicate_is_about():
    built = rules.build_rules(
        assignment_id=1,
        inbound_tags=["in"],
        categories=[],
        allow_list=[],
        block_list=["blocked.example"],
        strict_mode=True,
    )
    strict_rule = next(rule for rule in built if rules.parse_tag(rule["ruleTag"])[1] == "strict")

    assert strict_rule["ip"] == list(rules.ANY_IP)
    assert "domain" not in strict_rule
