from packaging.version import InvalidVersion, Version

MIN_NODE_VERSION = "0.6.19"
FILTER_ASSET_IMAGE = "v0.8.19-filter-assets"
CONNECTED_STATUS = "connected"
XRAY_CORE = "xray"

NODE_DISCONNECTED = "node_disconnected"
CORE_NOT_XRAY = "core_not_xray"
NODE_OUTDATED = "node_outdated"
PRE_ROUTED = "pre_routed"
NO_NODES = "no_nodes"

REASONS = (NODE_DISCONNECTED, CORE_NOT_XRAY, NODE_OUTDATED, PRE_ROUTED, NO_NODES)

REASON_TEXT = {
    NODE_DISCONNECTED: "is not connected, so the panel cannot confirm what its core would accept",
    CORE_NOT_XRAY: "does not run an xray core, so it has no routing engine that can carry a destination rule",
    NODE_OUTDATED: (
        f"runs a node image older than v{MIN_NODE_VERSION} (image {FILTER_ASSET_IMAGE}); its xray refuses a rule "
        "this large over the commander and carries no pgfilter.dat, so the core would fail to start"
    ),
    PRE_ROUTED: "already sends every connection on this endpoint to an outbound before a filter rule would see it",
    NO_NODES: "is served by no node",
}


def carries_filter_assets(node_version: str | None) -> bool:
    if not node_version:
        return False
    try:
        return Version(str(node_version)) >= Version(MIN_NODE_VERSION)
    except InvalidVersion:
        return False


def node_reason(status: str | None, core_type: str | None, node_version: str | None) -> str | None:
    if str(status or "") != CONNECTED_STATUS:
        return NODE_DISCONNECTED
    if str(core_type or "") != XRAY_CORE:
        return CORE_NOT_XRAY
    if not carries_filter_assets(node_version):
        return NODE_OUTDATED
    return None


def leading_reason(reasons) -> str | None:
    present = {reason for reason in reasons if reason}
    speaking = present - {NODE_DISCONNECTED}
    for reason in REASONS:
        if reason in (speaking or present):
            return reason
    return None


def node_note(node_id: int, name: str, reason: str) -> str:
    label = f"node {node_id}" if not name else f"node {node_id} ({name})"
    return f"{label} {REASON_TEXT[reason]}"


def tag_note(tag: str, reason: str) -> str:
    return f"endpoint {tag} {REASON_TEXT[reason]}"


NAMED_BLOCKERS = 5


def blocker_summary(blockers) -> str:
    listed = list(blockers)
    named = "; ".join(node_note(entry.id, entry.name, entry.reason) for entry in listed[:NAMED_BLOCKERS])
    rest = listed[NAMED_BLOCKERS:]
    if not rest:
        return named
    return f"{named}; and {len(rest)} more ({', '.join(str(entry.id) for entry in rest)})"
