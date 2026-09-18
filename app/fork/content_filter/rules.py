import hashlib
import hmac
import ipaddress
import json
from typing import NamedTuple

from app.fork.content_filter.catalog import expand

BLOCK_OUTBOUND = "BLOCK"
DIRECT_OUTBOUND = "DIRECT"
REFERENCED_OUTBOUNDS = (BLOCK_OUTBOUND, DIRECT_OUTBOUND)
BLOCKING_PROTOCOL = "blackhole"
DIRECT_PROTOCOL = "freedom"
DNS_PROTOCOL = "dns"
REDIRECT_SETTING = "redirect"
IP_ON_DEMAND = "ipondemand"
IP_RESOLVING_STRATEGIES = ("ipondemand", "ipifnonmatch")
BLOCKING_TAG_HINTS = (
    "api",
    "ban",
    "blackhole",
    "block",
    "blocked",
    "deny",
    "dns",
    "dns-out",
    "dns_out",
    "drop",
    "reject",
)
API_OUTBOUND = "api"
DIRECT_TAG_HINTS = ("direct", "freedom")
NON_DOMAIN_PREDICATES = ("port", "network", "protocol", "source", "sourcePort", "user", "attrs")
ROUTING_FIELDS = ("domain", "ip", *NON_DOMAIN_PREDICATES, "inboundTag", "balancerTag")
PRIVATE_GEOIP = "geoip:private"
DOMAIN_MATCHER_KINDS = ("domain", "full", "keyword", "regexp", "geosite", "ext")
NAMED_MATCHER_KINDS = ("geosite", "ext")
UNEXPANDABLE_MATCHER_KINDS = ("geosite", "ext", "regexp")
CLASH_REMEDY = (
    "Edit that rule in the core config so it no longer covers this filter's traffic. If it applies to every "
    "inbound, give it an inboundTag that leaves out the inbounds you are filtering. If it already names them "
    "alongside others, take those inbounds out of its inboundTag - but if they are the only ones it names, "
    "emptying the list makes it apply to every inbound instead, so remove the rule or narrow its matchers "
    "rather than its inboundTag. Either way you can instead drop the matcher named above from it. Moving it is "
    "not an option, because the panel always appends its filter rules after the ones already in the core."
)
ADVISORY_NOTE = (
    "These do not stop the filter and it has been applied. Each names a rule that could carry, or partly block, "
    "traffic this profile covers; scope or narrow it if you want the filter to decide that traffic."
)
TAG_PREFIX = "pgcf"
TAG_PARTS = ("allow", "block", "cat", "strict")
ALLOWED_MATCHER_PREFIXES = ("domain", "full", "geosite", "ext")
ANY_IP = ("0.0.0.0/0", "::/0")
DOMAIN_MAX = 253
LABEL_MAX = 63
IDENTITY_LEN = 10
CHECKSUM_LEN = 8
TOKEN_EXTRA_CHARS = "-_.@"
LABEL_EXTRA_CHARS = "-_"


class RuleValueError(ValueError):
    pass


def _short_digest(namespace: str, payload: bytes, length: int) -> str:
    seed = f"{TAG_PREFIX}|{namespace}|".encode()
    return hashlib.blake2s(seed + payload, digest_size=16).hexdigest()[:length]


def _checksum(body: str) -> str:
    return _short_digest("tag", body.encode(), CHECKSUM_LEN)


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def rule_tag(assignment_id: int, part: str, identity: str) -> str:
    if part not in TAG_PARTS:
        raise RuleValueError(f"unknown rule part {part!r}")
    body = f"{int(assignment_id)}-{part}-{identity}"
    return f"{TAG_PREFIX}-{body}-{_checksum(body)}"


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(ch in "0123456789abcdef" for ch in value)


def parse_tag(tag: str | None) -> tuple[int, str, str | None] | None:
    if not tag or not isinstance(tag, str):
        return None
    parts = tag.split("-")
    if parts[0] != TAG_PREFIX:
        return None
    if len(parts) not in (3, 5):
        return None
    raw_id, part = parts[1], parts[2]
    if not (raw_id.isascii() and raw_id.isdigit()) or part not in TAG_PARTS:
        return None
    if len(parts) == 3:
        return int(raw_id), part, None
    identity, checksum = parts[3], parts[4]
    if not _is_hex(identity, IDENTITY_LEN) or not _is_hex(checksum, CHECKSUM_LEN):
        return None
    if not hmac.compare_digest(_checksum(f"{raw_id}-{part}-{identity}"), checksum):
        return None
    return int(raw_id), part, identity


def owns_tag(tag: str | None) -> bool:
    return parse_tag(tag) is not None


def tag_assignment_id(tag: str) -> int | None:
    parsed = parse_tag(tag)
    return None if parsed is None else parsed[0]


def tag_identity(tag: str) -> str | None:
    parsed = parse_tag(tag)
    return None if parsed is None else parsed[2]


def _require_domain(value: str, raw: str, source: str) -> None:
    if len(value) > DOMAIN_MAX:
        raise RuleValueError(f"{source} entry {raw!r} is longer than {DOMAIN_MAX} characters")
    labels = value.split(".")
    if len(labels) < 2:
        raise RuleValueError(f"{source} entry {raw!r} is not a domain")
    for label in labels:
        if not label or len(label) > LABEL_MAX:
            raise RuleValueError(f"{source} entry {raw!r} has an empty or over-long label")
        if not all(ch.isalnum() or ch in LABEL_EXTRA_CHARS for ch in label):
            raise RuleValueError(f"{source} entry {raw!r} holds a character that cannot appear in a domain")


def _require_token(value: str, kind: str) -> str:
    if not value or not all((ch.isascii() and ch.isalnum()) or ch in TOKEN_EXTRA_CHARS for ch in value):
        raise RuleValueError(f"catalog {kind} {value!r} is not a usable xray reference")
    return value


def _entry_matcher(raw: str, source: str) -> str | None:
    value = (raw or "").strip().lower()
    if not value:
        return None
    if ":" in value:
        raise RuleValueError(
            f"{source} entry {raw!r} carries an xray matcher prefix; only plain domains are accepted, "
            f"optionally as '=exact.example' or '*.subtree.example'"
        )
    prefix = "domain"
    if value.startswith("="):
        prefix, value = "full", value[1:]
    elif value.startswith("*."):
        value = value[2:]
    bare = value.strip().strip(".")
    if not bare:
        return None
    _require_domain(bare, raw, source)
    return f"{prefix}:{bare}"


def _list_matchers(values: list[str], source: str) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        matcher = _entry_matcher(raw, source)
        if matcher is not None:
            out.append(matcher)
    return out


def _external_matcher(ref: str) -> str:
    parts = (ref or "").split(":")
    if len(parts) != 2:
        raise RuleValueError(f"catalog external list {ref!r} is not in 'file:list' form")
    return "ext:" + ":".join(_require_token(part, "external list") for part in parts)


def _category_matchers(categories: list[str]) -> list[str]:
    geosites, extra_domains, externals = expand(categories)
    out = [f"geosite:{_require_token(name, 'geosite')}" for name in geosites]
    out += [_external_matcher(ref) for ref in externals]
    for name in extra_domains:
        value = (name or "").strip().lower().strip(".")
        if not value:
            continue
        _require_domain(value, name, "catalog")
        out.append(f"domain:{value}")
    return out


def _outbounds(config: dict) -> list[dict]:
    entries = config.get("outbounds") if isinstance(config, dict) else None
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def core_outbound_tags(config: dict) -> set[str]:
    return {str(entry.get("tag") or "").strip() for entry in _outbounds(config)} - {""}


def missing_outbounds(config: dict) -> list[str]:
    present = core_outbound_tags(config)
    return [tag for tag in REFERENCED_OUTBOUNDS if tag not in present]


def _tags_with_protocol(config: dict, protocol: str) -> list[str]:
    found = [
        str(entry.get("tag") or "").strip()
        for entry in _outbounds(config)
        if str(entry.get("protocol") or "").strip().lower() == protocol
    ]
    return sorted(tag for tag in found if tag)


def _domain_strategy(config: dict | None) -> str:
    routing = config.get("routing") if isinstance(config, dict) else None
    strategy = routing.get("domainStrategy") if isinstance(routing, dict) else None
    return str(strategy or "").strip().casefold()


def resolves_before_matching(config: dict | None) -> bool:
    return _domain_strategy(config) == IP_ON_DEMAND


def strict_mode_effective(config: dict | None) -> bool:
    return _domain_strategy(config) in IP_RESOLVING_STRATEGIES


def _redirect_rewrites(value: str) -> bool:
    text = (value or "").strip()
    if not text:
        return False
    host, separator, port = text.rpartition(":")
    if not separator:
        return True
    if host.strip().strip("[]"):
        return True
    try:
        return int(port) != 0
    except ValueError:
        return True


def rewrites_destination(entry: dict) -> bool:
    settings = entry.get("settings") if isinstance(entry, dict) else None
    if not isinstance(settings, dict):
        return False
    return _redirect_rewrites(str(settings.get(REDIRECT_SETTING) or ""))


def direct_outbound_tags(config: dict) -> list[str]:
    found = [
        str(entry.get("tag") or "").strip()
        for entry in _outbounds(config)
        if str(entry.get("protocol") or "").strip().lower() == DIRECT_PROTOCOL and not rewrites_destination(entry)
    ]
    return sorted(tag for tag in found if tag)


def blocking_outbound_tags(config: dict) -> set[str]:
    return set(_tags_with_protocol(config, BLOCKING_PROTOCOL))


def harmless_outbound_tags(config: dict) -> set[str]:
    return blocking_outbound_tags(config) | set(_tags_with_protocol(config, DNS_PROTOCOL))


def resolve_outbound_tags(config: dict) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, tags in (
        ("block", _tags_with_protocol(config, BLOCKING_PROTOCOL)),
        ("direct", direct_outbound_tags(config)),
    ):
        if tags:
            found[key] = tags[0]
    return found


def build_rules(
    assignment_id: int,
    inbound_tags: list[str],
    categories: list[str],
    allow_list: list[str],
    block_list: list[str],
    strict_mode: bool,
) -> list[dict]:
    bound = [str(tag) for tag in inbound_tags or []]
    allow = _list_matchers(allow_list, "allow list")
    blocked_domains = _list_matchers(block_list, "block list")
    category_domains = _category_matchers(categories or [])
    strict = bool(strict_mode)

    identity = _short_digest(
        "identity",
        _canonical([int(assignment_id), bound, allow, blocked_domains, category_domains, strict]),
        IDENTITY_LEN,
    )
    scope: dict = {"inboundTag": bound} if bound else {}
    built: list[dict] = []

    if allow:
        built.append(
            {
                "type": "field",
                "ruleTag": rule_tag(assignment_id, "allow", identity),
                **scope,
                "domain": allow,
                "outboundTag": DIRECT_OUTBOUND,
            }
        )

    if blocked_domains:
        built.append(
            {
                "type": "field",
                "ruleTag": rule_tag(assignment_id, "block", identity),
                **scope,
                "domain": blocked_domains,
                "outboundTag": BLOCK_OUTBOUND,
            }
        )

    if category_domains:
        built.append(
            {
                "type": "field",
                "ruleTag": rule_tag(assignment_id, "cat", identity),
                **scope,
                "domain": category_domains,
                "outboundTag": BLOCK_OUTBOUND,
            }
        )

    if strict and built:
        built.append(
            {
                "type": "field",
                "ruleTag": rule_tag(assignment_id, "strict", identity),
                **scope,
                "ip": list(ANY_IP),
                "outboundTag": BLOCK_OUTBOUND,
            }
        )

    return built


def digest(rules: list[dict]) -> str:
    payload = json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _snake(name: str) -> str:
    return "".join(f"_{ch.lower()}" if ch.isupper() else ch for ch in name)


def _field(rule: dict, name: str):
    if name in rule:
        return rule[name]
    return rule.get(_snake(name))


def _values(rule: dict, name: str) -> list[str]:
    value = _field(rule, name)
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [str(value)]


def _split_matcher(value: str) -> tuple[str, str]:
    text = (value or "").strip().lower()
    if not text:
        return "", ""
    head, separator, rest = text.partition(":")
    if separator and head in DOMAIN_MATCHER_KINDS:
        return head, rest
    return "keyword", text


def _suffixes(value: str) -> list[str]:
    parts = value.split(".")
    return [".".join(parts[index:]) for index in range(len(parts))]


def _own_domain_index(filter_rules: list[dict], allow: bool) -> tuple[set[str], set[str], set[str]]:
    tree: set[str] = set()
    full: set[str] = set()
    named: set[str] = set()
    for rule in filter_rules:
        parsed = parse_tag(str(_field(rule, "ruleTag") or ""))
        if (parsed is not None and parsed[1] == "allow") != allow:
            continue
        for value in _values(rule, "domain"):
            kind, rest = _split_matcher(value)
            if not rest:
                continue
            if kind == "domain":
                tree.add(rest)
            elif kind == "full":
                full.add(rest)
            elif kind in ("geosite", "ext"):
                named.add(f"{kind}:{rest}")
    return tree, full, named


def _category_label(named: set[str]) -> str:
    ordered = sorted(named)
    if len(ordered) == 1:
        return ordered[0]
    return f"{ordered[0]} and {len(ordered) - 1} more"


def _unresolvable(value: str, kind: str, tree: set[str], named: set[str]) -> str | None:
    if named:
        return f"{value} against {_category_label(named)}"
    if kind in UNEXPANDABLE_MATCHER_KINDS:
        return value
    if kind == "keyword" and tree:
        return value
    return None


def _domain_overlap(values: list[str], index: tuple[set[str], set[str], set[str]]) -> tuple[str, bool] | None:
    tree, full, named = index
    concrete = tree | full
    if not concrete and not named:
        return None
    theoretical: str | None = None
    for value in values:
        kind, rest = _split_matcher(value)
        if not rest:
            continue
        if kind == "full":
            if rest in full or any(suffix in tree for suffix in _suffixes(rest)):
                return value, True
        elif kind == "domain":
            if any(suffix in tree for suffix in _suffixes(rest)):
                return value, True
            if any(candidate == rest or candidate.endswith(f".{rest}") for candidate in concrete):
                return value, True
        elif kind == "keyword":
            if any(rest in candidate for candidate in concrete):
                return value, True
        elif kind in NAMED_MATCHER_KINDS and f"{kind}:{rest}" in named:
            return value, True
        if theoretical is None:
            theoretical = _unresolvable(value, kind, tree, named)
    return (theoretical, False) if theoretical is not None else None


def _rule_label(body: dict, bound: frozenset, scope: set) -> str:
    named = str(_field(body, "ruleTag") or "")
    if named:
        return named
    if not bound:
        return "a rule that applies to every inbound"
    covered = sorted(bound & scope) if scope else sorted(bound)
    if not covered:
        return "a rule that applies to every inbound"
    return "a rule whose inboundTag already names " + ", ".join(covered)


def _destination(outbound: str, body: dict) -> str:
    if outbound:
        return outbound
    balancer = str(_field(body, "balancerTag") or "").strip()
    return f"balancer {balancer}" if balancer else "the default outbound"


def _has_allow_matchers(filter_rules: list[dict]) -> bool:
    for rule in filter_rules:
        parsed = parse_tag(str(_field(rule, "ruleTag") or ""))
        if parsed is not None and parsed[1] == "allow" and _values(rule, "domain"):
            return True
    return False


def _scope_meets(filter_bound: list[str], existing_bound: frozenset) -> bool:
    if not filter_bound or not existing_bound:
        return True
    return bool(set(filter_bound) & existing_bound)


def _core_rules(config: dict | None) -> list[dict]:
    routing = config.get("routing") if isinstance(config, dict) else None
    entries = routing.get("rules") if isinstance(routing, dict) else None
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _describes_itself(rule: dict) -> bool:
    return any(_values(rule, name) for name in ROUTING_FIELDS)


def _harmless_exemptions(config: dict | None) -> tuple[set[str], set[str]]:
    if config is None:
        return set(), {hint.casefold() for hint in BLOCKING_TAG_HINTS}
    exempt = harmless_outbound_tags(config)
    declared = core_outbound_tags(config)
    spellings = {API_OUTBOUND, API_OUTBOUND.upper()}
    if not spellings & declared:
        exempt |= spellings
    return exempt, set()


def _direct_exemptions(config: dict | None) -> tuple[set[str], set[str]]:
    if config is None:
        return set(), {hint.casefold() for hint in DIRECT_TAG_HINTS}
    return set(direct_outbound_tags(config)), set()


def _matches(tag: str, exact: set[str], by_name: set[str]) -> bool:
    return tag in exact or tag.casefold() in by_name


def _non_public_ip(value: str) -> bool:
    text = (value or "").strip().lower()
    if text == PRIVATE_GEOIP:
        return True
    if text.startswith(("geoip:", "ext:")):
        return False
    try:
        return not ipaddress.ip_network(text, strict=False).is_global
    except ValueError:
        return False


class RuleOverlap(NamedTuple):
    clashes: list[str]
    advisories: list[str]


def _blocking_reason(body: dict, blocked: tuple, strict: bool, resolved_first: bool) -> tuple[str, bool] | None:
    domains = _values(body, "domain")
    if domains:
        hit = _domain_overlap(domains, blocked)
        if hit is not None:
            return hit[0], hit[1] or strict
        return ("traffic that strict mode blocks", True) if strict else None

    addresses = _values(body, "ip")
    if addresses:
        if not strict and not resolved_first and all(_non_public_ip(value) for value in addresses):
            return None
        return f"traffic for {addresses[0]}", True

    for name in NON_DOMAIN_PREDICATES:
        values = _values(body, name)
        if values:
            return f"every connection matching {name} {values[0]}", True
    return "every connection", True


def _networks(body: dict) -> set[str]:
    listed: set[str] = set()
    for value in _values(body, "network"):
        for part in str(value).split(","):
            text = part.strip().lower()
            if text:
                listed.add(text)
    return listed


def _allow_reason(body: dict, allowed: tuple, resolved_first: bool) -> tuple[str, bool] | None:
    if not any(allowed):
        return None
    whole = not _values(body, "port") and not _networks(body)
    domains = _values(body, "domain")
    if domains:
        hit = _domain_overlap(domains, allowed)
        return None if hit is None else (hit[0], hit[1] and whole)

    addresses = _values(body, "ip")
    if addresses:
        if not resolved_first and all(_non_public_ip(value) for value in addresses):
            return None
        return f"traffic for {addresses[0]}", whole

    for name in NON_DOMAIN_PREDICATES:
        values = _values(body, name)
        if values:
            return f"every connection matching {name} {values[0]}", whole
    return "every connection", True


def _overlap_verdict(
    body: dict, blocked: tuple, allowed: tuple, strict: bool, harmless: bool, direct: bool, resolved_first: bool
):
    if not harmless and (strict or any(blocked)):
        verdict = _blocking_reason(body, blocked, strict, resolved_first)
        if verdict is not None:
            return verdict[0], verdict[1], False
    if direct:
        return None
    hit = _allow_reason(body, allowed, resolved_first)
    return None if hit is None else (hit[0], hit[1], True)


def conflicting_rules(
    existing: list[dict],
    inbound_tags: list[str],
    config: dict | None,
    filter_rules: list[dict],
) -> RuleOverlap:
    clashes: list[str] = []
    advisories: list[str] = []
    if not filter_rules:
        return RuleOverlap(clashes, advisories)

    scope = set(inbound_tags)
    exempt, exempt_by_name = _harmless_exemptions(config)
    direct_tags, direct_by_name = _direct_exemptions(config)

    saved = [entry for entry in _core_rules(config) if not owns_tag(str(_field(entry, "ruleTag") or ""))]
    by_tag = {str(_field(entry, "ruleTag") or ""): entry for entry in saved if _field(entry, "ruleTag")}

    per_scope: dict[frozenset, tuple] = {}

    resolved_first = resolves_before_matching(config)
    protects_allow = _has_allow_matchers(filter_rules)
    can_be_bypassed = bool(
        any(_own_domain_index(filter_rules, False))
        or {value for rule in filter_rules for value in _values(rule, "ip")} & set(ANY_IP)
    )
    identified: list[tuple[str, dict]] = []
    stubs: dict[str, list[dict]] = {}
    for rule in existing:
        tag = str(_field(rule, "ruleTag") or "")
        if owns_tag(tag):
            continue
        outbound = str(_field(rule, "outboundTag") or "")
        if _describes_itself(rule):
            identified.append((outbound, rule))
        elif tag and tag in by_tag:
            identified.append((outbound, by_tag[tag]))
        else:
            stubs.setdefault(outbound, []).append(rule)

    accounted = {str(_field(body, "ruleTag") or "") for _, body in identified if _field(body, "ruleTag")}
    for outbound, summaries in stubs.items():
        candidates = [
            entry
            for entry in saved
            if str(_field(entry, "outboundTag") or "") == outbound
            and str(_field(entry, "ruleTag") or "") not in accounted
        ]
        identified.extend((outbound, entry) for entry in candidates)
        if len(summaries) <= len(candidates):
            continue
        destination = _destination(outbound, summaries[0])
        if not _matches(outbound, exempt, exempt_by_name) and can_be_bypassed:
            clashes.append(f"an unidentified rule sends every connection to {destination}")
        elif protects_allow and not _matches(outbound, direct_tags, direct_by_name):
            clashes.append(
                f"an unidentified rule sends every connection to {destination}, "
                "blocking a destination this profile's allow list permits"
            )

    seen: set[str] = set()
    for outbound, body in identified:
        bound = frozenset(_values(body, "inboundTag"))
        if scope and bound and not (scope & bound):
            continue
        if bound not in per_scope:
            reaching = [rule for rule in filter_rules if _scope_meets(_values(rule, "inboundTag"), bound)]
            per_scope[bound] = (
                _own_domain_index(reaching, False),
                _own_domain_index(reaching, True),
                bool({value for rule in reaching for value in _values(rule, "ip")} & set(ANY_IP)),
            )
        blocked, allowed, strict = per_scope[bound]
        if not any(blocked) and not any(allowed) and not strict:
            continue
        harmless = _matches(outbound, exempt, exempt_by_name)
        if harmless and not any(allowed):
            continue
        verdict = _overlap_verdict(
            body, blocked, allowed, strict, harmless, _matches(outbound, direct_tags, direct_by_name), resolved_first
        )
        if verdict is None:
            continue
        reason, blocking, overrides_allow = verdict
        label = _rule_label(body, bound, scope)
        destination = _destination(outbound, body)
        if overrides_allow:
            scale = "" if blocking else "part of "
            message = (
                f"{label} sends {reason} to {destination}, "
                f"blocking {scale}a destination this profile's allow list permits"
            )
        elif blocking:
            message = f"{label} sends {reason} to {destination}"
        else:
            message = f"{label} could send a hostname matching {reason} to {destination}"
        if message in seen:
            continue
        seen.add(message)
        (clashes if blocking else advisories).append(message)
    return RuleOverlap(clashes, advisories)
