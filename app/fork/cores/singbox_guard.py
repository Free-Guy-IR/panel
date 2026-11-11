import json

from packaging.version import InvalidVersion, Version

from app.models.core import CoreType

SINGBOX_NAME_KEYED_INBOUND_TYPES = frozenset({"hysteria2"})

SINGBOX_UNMANAGED_INBOUND_TYPES = frozenset(
    {
        "direct",
        "mixed",
        "socks",
        "http",
        "tun",
        "redirect",
        "tproxy",
        "shadowtls",
    }
)

SINGBOX_NAME_KEYED_SINCE_NODE_VERSION: dict[str, str | None] = {
    "vless": None,
    "vmess": None,
    "trojan": None,
    "shadowsocks": None,
    "tuic": None,
}

_DEFECT = (
    "outside hysteria2 a sing-box node binds each user's traffic accounting to that user's position in "
    "the inbound's user list, and it re-sorts that list by name every time a user is added or removed, so "
    "creating or deleting a single account silently re-points a live session's usage onto a different "
    "customer, and a large enough deletion reads past the end of the list and kills the node process"
)

_REMEDY = (
    "Remove those inbounds from this core, or serve those protocols from an Xray core, which keys usage "
    "by the user's email instead of a list position."
)

NAMED_BLOCKERS = 5


def inbound_is_always_name_keyed(inbound_type: str) -> bool:
    return inbound_type in SINGBOX_NAME_KEYED_INBOUND_TYPES or inbound_type in SINGBOX_UNMANAGED_INBOUND_TYPES


def name_keyed_since(inbound_type: str) -> str | None:
    return SINGBOX_NAME_KEYED_SINCE_NODE_VERSION.get(inbound_type)


def inbound_is_name_keyed_on(inbound_type: str, node_version: str | None) -> bool:
    if inbound_is_always_name_keyed(inbound_type):
        return True
    minimum = name_keyed_since(inbound_type)
    if not minimum or not node_version:
        return False
    try:
        return Version(str(node_version)) >= Version(minimum)
    except InvalidVersion:
        return False


UNREADABLE_CONFIG = ("<whole config>", "unreadable")


def index_keyed_inbounds(config) -> list[tuple[str, str]]:
    if not isinstance(config, dict):
        return [UNREADABLE_CONFIG]

    inbounds = config.get("inbounds")
    if inbounds is None:
        return []
    if not isinstance(inbounds, list):
        return [UNREADABLE_CONFIG]

    found: list[tuple[str, str]] = []
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            found.append(UNREADABLE_CONFIG)
            continue
        inbound_type = str(inbound.get("type") or "")
        if inbound_is_always_name_keyed(inbound_type):
            continue
        found.append((str(inbound.get("tag") or inbound_type or "untagged"), inbound_type or "untyped"))
    return found


def never_name_keyed(offenders: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(tag, inbound_type) for tag, inbound_type in offenders if not name_keyed_since(inbound_type)]


def not_name_keyed_on(offenders: list[tuple[str, str]], node_version: str | None) -> list[tuple[str, str]]:
    return [
        (tag, inbound_type)
        for tag, inbound_type in offenders
        if not inbound_is_name_keyed_on(inbound_type, node_version)
    ]


def stale_singbox_nodes(offenders: list[tuple[str, str]], node_versions) -> list[tuple[str, str | None]]:
    return [(name, version) for name, version in node_versions if not_name_keyed_on(offenders, version)]


def unknown_type_note(offenders: list[tuple[str, str]]) -> str:
    unknown = sorted(
        {
            inbound_type
            for _, inbound_type in offenders
            if inbound_type not in SINGBOX_NAME_KEYED_SINCE_NODE_VERSION and inbound_type != UNREADABLE_CONFIG[1]
        }
    )
    if not unknown:
        return ""
    return (
        f" The inbound type(s) {', '.join(unknown)} are not known to this panel at all, so it cannot confirm "
        "they resolve users by name and refuses them rather than guessing."
    )


def gate_reason(node_version: str | None) -> str:
    if node_version:
        return f"this node runs image v{node_version}, which does not resolve those users by name"
    return "this node reported no image version, so the panel cannot confirm it resolves those users by name"


def blocker_reason(blockers: list[tuple[str, str | None]]) -> str:
    named = ", ".join(
        f"{name} (v{version})" if version else f"{name} (no reported version)"
        for name, version in blockers[:NAMED_BLOCKERS]
    )
    rest = len(blockers) - NAMED_BLOCKERS
    if rest > 0:
        named = f"{named} and {rest} more"
    return f"nodes already running this core do not resolve those users by name: {named}"


def unsafe_inbounds_message(
    subject: str,
    offenders: list[tuple[str, str]],
    node_version: str | None = None,
    blockers: list[tuple[str, str | None]] | None = None,
    no_build: bool = False,
) -> str:
    listed = ", ".join(f"{tag} ({inbound_type})" for tag, inbound_type in offenders)
    if no_build:
        reason = "no node build is known to resolve those users by name, so the panel refuses them outright"
    elif blockers:
        reason = blocker_reason(blockers)
    else:
        reason = gate_reason(node_version)
    return (
        f"{subject} declares the sing-box inbound(s) {listed}, which this panel refuses to put on a node. "
        f"Only hysteria2 is safe on a sing-box core: {_DEFECT}. "
        f"This refusal is a deliberate safety gate, not a bug — {reason}.{unknown_type_note(offenders)} {_REMEDY}"
    )


def singbox_core_subject(core_name: str | None) -> str:
    return f'Core "{core_name}"' if core_name else "This core"


def unsafe_singbox_core_message(core_type, core_name: str | None, config) -> str | None:
    if core_type != CoreType.singbox:
        return None
    offenders = never_name_keyed(index_keyed_inbounds(config))
    if not offenders:
        return None
    return unsafe_inbounds_message(singbox_core_subject(core_name), offenders, no_build=True)


def unsafe_singbox_core_nodes_message(core_type, core_name: str | None, config, node_versions) -> str | None:
    if core_type != CoreType.singbox:
        return None
    offenders = index_keyed_inbounds(config)
    if not offenders:
        return None
    blockers = stale_singbox_nodes(offenders, node_versions)
    if not blockers:
        return None
    return unsafe_inbounds_message(singbox_core_subject(core_name), offenders, blockers=blockers)


def dispatched_config(core):
    to_str = getattr(core, "to_str", None)
    if not callable(to_str):
        return core
    try:
        return json.loads(to_str())
    except Exception:
        return None


def singbox_index_keyed_inbounds(core) -> list[tuple[str, str]]:
    if getattr(core, "type", None) != CoreType.singbox:
        return []
    return index_keyed_inbounds(dispatched_config(core))


def unsafe_singbox_node_message(subject: str, offenders: list[tuple[str, str]], node_version: str | None) -> str | None:
    blocked = not_name_keyed_on(offenders, node_version)
    if not blocked:
        return None
    return unsafe_inbounds_message(subject, blocked, node_version)


SINGBOX_MATCHER_DOMAIN_FIELDS = ("domain", "domain_suffix")
SINGBOX_MATCHER_MARKERS = ("\n", "\r")


def _entries(mapping, name: str) -> list[tuple[str, object]]:
    if not isinstance(mapping, dict):
        return []
    return [(key, value) for key, value in mapping.items() if isinstance(key, str) and key.casefold() == name]


def _marked_domain_values(rule: dict, path: str) -> list[str]:
    found = []
    for field in SINGBOX_MATCHER_DOMAIN_FIELDS:
        for key, values in _entries(rule, field):
            items = [values] if isinstance(values, str) else values if isinstance(values, list) else []
            found.extend(
                f"{path}.{key}[{index}]"
                for index, value in enumerate(items)
                if isinstance(value, str) and any(marker in value for marker in SINGBOX_MATCHER_MARKERS)
            )
    return found


def _rules_with_marked_domains(rules, path: str) -> list[str]:
    if not isinstance(rules, list):
        return []
    found = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        rule_path = f"{path}[{index}]"
        found.extend(_marked_domain_values(rule, rule_path))
        for key, nested in _entries(rule, "rules"):
            found.extend(_rules_with_marked_domains(nested, f"{rule_path}.{key}"))
    return found


def domain_rule_line_breaks(config) -> list[str]:
    found = []
    for route_key, route in _entries(config, "route"):
        for key, rules in _entries(route, "rules"):
            found.extend(_rules_with_marked_domains(rules, f"{route_key}.{key}"))
        for set_key, rule_sets in _entries(route, "rule_set"):
            for index, rule_set in enumerate(rule_sets if isinstance(rule_sets, list) else []):
                for key, rules in _entries(rule_set, "rules"):
                    found.extend(_rules_with_marked_domains(rules, f"{route_key}.{set_key}[{index}].{key}"))
    for dns_key, dns in _entries(config, "dns"):
        for key, rules in _entries(dns, "rules"):
            found.extend(_rules_with_marked_domains(rules, f"{dns_key}.{key}"))
    return found


def domain_rule_line_break_message(subject: str, *configs) -> str | None:
    offenders = list(dict.fromkeys(path for config in configs for path in domain_rule_line_breaks(config)))
    if not offenders:
        return None
    return (
        f"{subject} has a line break (\\n or \\r) in a sing-box domain or domain_suffix value: "
        f"{', '.join(offenders)}. sing-box uses these characters as markers in its domain matcher, so such a value "
        "can crash sing-box while it loads the rules or change which domains the rule matches; remove it."
    )


def singbox_template_line_break_message(content: str, rendered) -> str | None:
    try:
        served = json.loads(content)
    except ValueError:
        served = None
    return domain_rule_line_break_message("The sing-box subscription template", served, rendered)
