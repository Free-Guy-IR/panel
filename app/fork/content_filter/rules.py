import hashlib
import json

from app.fork.content_filter.catalog import expand

BLOCK_OUTBOUND = "BLOCK"
DIRECT_OUTBOUND = "DIRECT"
TAG_PREFIX = "pgcf"
ANY_IP = ("0.0.0.0/0", "::/0")


def rule_tag(assignment_id: int, part: str) -> str:
    return f"{TAG_PREFIX}-{assignment_id}-{part}"


def owns_tag(tag: str | None) -> bool:
    return bool(tag) and tag.startswith(f"{TAG_PREFIX}-")


def tag_assignment_id(tag: str) -> int | None:
    parts = tag.split("-")
    if len(parts) < 3 or parts[0] != TAG_PREFIX:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def _domain_matchers(values: list[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        value = (raw or "").strip().lower()
        if not value:
            continue
        if ":" in value:
            out.append(value)
        else:
            out.append(f"domain:{value}")
    return out


def build_rules(
    assignment_id: int,
    inbound_tags: list[str],
    categories: list[str],
    allow_list: list[str],
    block_list: list[str],
    strict_mode: bool,
) -> list[dict]:
    scope: dict = {"inboundTag": inbound_tags} if inbound_tags else {}
    rules: list[dict] = []

    allow = _domain_matchers(allow_list)
    if allow:
        rules.append(
            {
                "type": "field",
                "ruleTag": rule_tag(assignment_id, "allow"),
                **scope,
                "domain": allow,
                "outboundTag": DIRECT_OUTBOUND,
            }
        )

    blocked_domains = _domain_matchers(block_list)
    if blocked_domains:
        rules.append(
            {
                "type": "field",
                "ruleTag": rule_tag(assignment_id, "block"),
                **scope,
                "domain": blocked_domains,
                "outboundTag": BLOCK_OUTBOUND,
            }
        )

    geosites = [f"geosite:{name}" for name in expand(categories)]
    if geosites:
        rules.append(
            {
                "type": "field",
                "ruleTag": rule_tag(assignment_id, "cat"),
                **scope,
                "domain": geosites,
                "outboundTag": BLOCK_OUTBOUND,
            }
        )

    if strict_mode and rules:
        rules.append(
            {
                "type": "field",
                "ruleTag": rule_tag(assignment_id, "strict"),
                **scope,
                "ip": list(ANY_IP),
                "outboundTag": BLOCK_OUTBOUND,
            }
        )

    return rules


def digest(rules: list[dict]) -> str:
    payload = json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def conflicting_rules(existing: list[dict], inbound_tags: list[str]) -> list[str]:
    scope = set(inbound_tags)
    clashes: list[str] = []
    for rule in existing:
        tag = rule.get("ruleTag") or rule.get("rule_tag") or ""
        if owns_tag(tag):
            continue
        outbound = (rule.get("outboundTag") or rule.get("outbound_tag") or "").upper()
        if outbound in {"API", BLOCK_OUTBOUND}:
            continue
        bound = rule.get("inboundTag") or rule.get("inbound_tag") or []
        if isinstance(bound, str):
            bound = [bound]
        if not bound or (scope and scope & set(bound)):
            clashes.append(tag or "<untagged>")
    return clashes
