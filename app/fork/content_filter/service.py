import json
from copy import deepcopy
from datetime import UTC, datetime as dt

from PasarGuardNodeBridge import NodeAPIError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.core import get_core_config_by_id
from app.db.models import Node
from app.fork.content_filter.rules import (
    build_rules,
    conflicting_rules,
    digest,
    owns_tag,
    tag_assignment_id,
)
from app.fork.models.content_filter import ContentFilterAssignment
from app.node import node_manager
from app.utils.logger import get_logger

logger = get_logger("content-filter")

UNMATCHED_MARKER = "not enough information"
PROBE_DOMAIN = "www.pornhub.com"


class EnforcementError(Exception):
    def __init__(self, detail: str, code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.code = code


def assignment_rules(assignment: ContentFilterAssignment) -> list[dict]:
    profile = assignment.profile
    tags = [assignment.inbound_tag] if assignment.inbound_tag else []
    return build_rules(
        assignment_id=assignment.id,
        inbound_tags=tags,
        categories=list(profile.categories or []),
        allow_list=list(profile.allow_list or []),
        block_list=list(profile.block_list or []),
        strict_mode=bool(profile.strict_mode),
    )


async def node_inbound_tags(db: AsyncSession, node_id: int) -> set[str]:
    node = await db.get(Node, node_id)
    if node is None:
        return set()
    core = await get_core_config_by_id(db, node.core_config_id)
    if core is None:
        return set()
    config = core.config if isinstance(core.config, dict) else json.loads(core.config or "{}")
    return {str(i.get("tag") or "") for i in config.get("inbounds") or [] if i.get("tag")}


async def _assignments_for_node(db: AsyncSession, node_id: int) -> list[ContentFilterAssignment]:
    result = await db.execute(
        select(ContentFilterAssignment).where(
            ContentFilterAssignment.is_enabled.is_(True),
            or_(
                ContentFilterAssignment.node_id == node_id,
                ContentFilterAssignment.node_id.is_(None),
            ),
        )
    )
    rows = list(result.scalars().all())
    pinned = [a for a in rows if a.node_id is not None]
    floating = [a for a in rows if a.node_id is None]
    if not floating:
        return pinned
    tags = await node_inbound_tags(db, node_id)
    return pinned + [a for a in floating if a.inbound_tag in tags]


async def nodes_for_assignment(db: AsyncSession, assignment: ContentFilterAssignment) -> list[int]:
    if assignment.node_id is not None:
        return [assignment.node_id]
    nodes = (await db.execute(select(Node))).scalars().all()
    reach: list[int] = []
    for node in nodes:
        if assignment.inbound_tag in await node_inbound_tags(db, node.id):
            reach.append(node.id)
    return reach


async def _nodes_on_core(db: AsyncSession, core_id: int) -> list[Node]:
    result = await db.execute(select(Node).where(Node.core_config_id == core_id))
    return list(result.scalars().all())


async def core_rules(db: AsyncSession, core_id: int) -> list[dict]:
    rules: list[dict] = []
    seen: set[str] = set()
    for node in await _nodes_on_core(db, core_id):
        for assignment in await _assignments_for_node(db, node.id):
            for rule in assignment_rules(assignment):
                tag = rule.get("ruleTag") or ""
                if tag in seen:
                    continue
                seen.add(tag)
                rules.append(rule)
    return rules


def _strip_owned(rules: list[dict]) -> list[dict]:
    return [rule for rule in rules if not owns_tag(rule.get("ruleTag"))]


SNIFFING = {"enabled": True, "destOverride": ["http", "tls", "quic"], "routeOnly": False}


def _scoped_tags(rules: list[dict]) -> set[str]:
    tags: set[str] = set()
    for rule in rules:
        for tag in rule.get("inboundTag") or []:
            tags.add(tag)
    return tags


def build_core_config(existing: dict, filter_rules: list[dict]) -> dict:
    config = deepcopy(existing)
    routing = config.get("routing")
    if not isinstance(routing, dict):
        routing = {}
        config["routing"] = routing
    current = routing.get("rules")
    current = list(current) if isinstance(current, list) else []
    routing["rules"] = filter_rules + _strip_owned(current)

    scoped = _scoped_tags(filter_rules)
    whole_core = bool(filter_rules) and not scoped
    for inbound in config.get("inbounds") or []:
        tag = str(inbound.get("tag") or "")
        if tag == "API_INBOUND":
            continue
        if whole_core or tag in scoped:
            inbound["sniffing"] = dict(SNIFFING)
    return config


async def persist_core_rules(db: AsyncSession, core_id: int, admin) -> bool:
    from app.models.core import CoreCreate
    from app.operation import OperatorType
    from app.operation.core import CoreOperation

    db_core = await get_core_config_by_id(db, core_id)
    if db_core is None:
        raise EnforcementError(f"core {core_id} not found", code=404)

    filter_rules = await core_rules(db, core_id)
    current = db_core.config if isinstance(db_core.config, dict) else json.loads(db_core.config or "{}")
    updated = build_core_config(current, filter_rules)
    if updated == current:
        return False
    inbounds_changed = {
        str(i.get("tag") or "")
        for i in updated.get("inbounds") or []
        if i.get("sniffing")
        != next(
            (j.get("sniffing") for j in current.get("inbounds") or [] if str(j.get("tag") or "") == str(i.get("tag") or "")),
            None,
        )
    }

    operator = CoreOperation(operator_type=OperatorType.SYSTEM)
    await operator.modify_core(
        db,
        core_id,
        CoreCreate(
            name=db_core.name,
            config=updated,
            type=db_core.type,
            exclude_inbound_tags=set(db_core.exclude_inbound_tags or []),
            fallbacks_inbound_tags=set(db_core.fallbacks_inbound_tags or []),
        ),
        admin,
    )
    if inbounds_changed:
        logger.info(f"core {core_id}: name recovery enabled on {sorted(inbounds_changed)}; nodes must reload to pick it up")
        await _restart_core_nodes(db, core_id, admin)
    return True


async def _restart_core_nodes(db: AsyncSession, core_id: int, admin) -> None:
    from app.operation import OperatorType
    from app.operation.node import NodeOperation

    operator = NodeOperation(operator_type=OperatorType.SYSTEM)
    try:
        await operator.restart_all_node(db=db, core_id=core_id, admin=admin)
    except Exception as exc:
        logger.warning(f"core {core_id}: could not restart its nodes after enabling name recovery: {exc}")


async def live_rules(node_id: int) -> list[dict]:
    node = await node_manager.get_node(node_id)
    if node is None:
        raise EnforcementError(f"node {node_id} is not attached to this panel", code=404)
    try:
        response = await node.list_routing_rules()
    except NodeAPIError as exc:
        if exc.code == 501:
            raise EnforcementError(
                "this node's core does not publish RoutingService; add "
                '"api": {"services": ["RoutingService"]} to its core config and restart it',
                code=409,
            ) from exc
        raise EnforcementError(exc.detail or str(exc), code=exc.code if exc.code > 0 else 503) from exc
    return [{"ruleTag": rule.rule_tag or "", "outboundTag": rule.outbound_tag or ""} for rule in (response.rules or [])]


async def _remove_owned(node, tags: list[str]) -> None:
    for tag in tags:
        try:
            await node.remove_routing_rule(rule_tag=tag)
        except NodeAPIError as exc:
            logger.warning(f"could not remove routing rule {tag}: {exc.detail}")


async def push_live(db: AsyncSession, node_id: int) -> list[dict]:
    node = await node_manager.get_node(node_id)
    if node is None:
        raise EnforcementError(f"node {node_id} is not attached to this panel", code=404)

    existing = await live_rules(node_id)
    stale = [rule["ruleTag"] for rule in existing if owns_tag(rule["ruleTag"])]
    await _remove_owned(node, stale)

    wanted: list[dict] = []
    for assignment in await _assignments_for_node(db, node_id):
        wanted.extend(assignment_rules(assignment))

    if wanted:
        remaining = [rule for rule in await live_rules(node_id) if not owns_tag(rule["ruleTag"])]
        scope = {tag for rule in wanted for tag in rule.get("inboundTag", [])}
        clashes = conflicting_rules(remaining, sorted(scope))
        if clashes:
            raise EnforcementError(
                "a rule already on this node is evaluated before the filter and could carry the same traffic: "
                + ", ".join(clashes[:5]),
                code=409,
            )

    for rule in wanted:
        try:
            await node.add_routing_rule(rule=json.dumps(rule), should_reset=False)
        except NodeAPIError as exc:
            raise EnforcementError(exc.detail or str(exc), code=exc.code if exc.code > 0 else 503) from exc
    return wanted


async def probe(node_id: int, inbound_tag: str, domain: str) -> str:
    node = await node_manager.get_node(node_id)
    if node is None:
        return "node-unavailable"
    try:
        result = await node.test_route(
            inbound_tag=inbound_tag, network="tcp", target_domain=domain, target_port=443
        )
    except NodeAPIError as exc:
        if UNMATCHED_MARKER in (exc.detail or ""):
            return "unmatched"
        return f"error:{exc.detail}"
    return result.outbound_tag if result else "unmatched"


async def apply_assignment(db: AsyncSession, assignment: ContentFilterAssignment, admin) -> dict:
    node_ids = await nodes_for_assignment(db, assignment)
    if not node_ids:
        assignment.enforced = False
        assignment.last_error = "no node currently carries that endpoint"
        assignment.last_checked_at = dt.now(UTC)
        await db.commit()
        raise EnforcementError(assignment.last_error, code=404)

    core_ids: set[int] = set()
    for node_id in node_ids:
        node = await db.get(Node, node_id)
        if node is not None:
            core_ids.add(node.core_config_id)
    for core_id in core_ids:
        await persist_core_rules(db, core_id, admin)

    wanted = assignment_rules(assignment)
    assignment.applied_digest = digest(wanted)
    problems: list[str] = []
    reached = 0

    for node_id in node_ids:
        try:
            await push_live(db, node_id)
        except EnforcementError as exc:
            problems.append(f"node {node_id}: {exc.detail}")
            continue
        present = {rule["ruleTag"] for rule in await live_rules(node_id)}
        missing = [rule["ruleTag"] for rule in wanted if rule["ruleTag"] not in present]
        if missing:
            problems.append(f"node {node_id}: rules did not appear ({', '.join(missing)})")
        else:
            reached += 1

    assignment.enforced = reached == len(node_ids) and not problems
    assignment.last_error = "; ".join(problems)[:1024] if problems else None
    assignment.last_checked_at = dt.now(UTC)
    await db.commit()

    if not assignment.enforced:
        raise EnforcementError(assignment.last_error or "could not confirm enforcement", code=502)

    verdict = await probe(node_ids[0], assignment.inbound_tag, PROBE_DOMAIN)
    return {"rules": len(wanted), "nodes": reached, "probe": verdict}


async def withdraw_assignment(db: AsyncSession, assignment: ContentFilterAssignment, admin) -> None:
    node_ids = await nodes_for_assignment(db, assignment)
    core_ids: set[int] = set()
    for node_id in node_ids:
        node = await db.get(Node, node_id)
        if node is not None:
            core_ids.add(node.core_config_id)

    await db.delete(assignment)
    await db.commit()

    for core_id in core_ids:
        await persist_core_rules(db, core_id, admin)
    for node_id in node_ids:
        try:
            await push_live(db, node_id)
        except EnforcementError as exc:
            logger.warning(f"withdraw: live cleanup on node {node_id} failed: {exc.detail}")


async def reconcile_node(db: AsyncSession, node_id: int) -> str | None:
    assignments = await _assignments_for_node(db, node_id)
    if not assignments:
        return None

    try:
        live = await live_rules(node_id)
    except EnforcementError as exc:
        for assignment in assignments:
            assignment.enforced = False
            assignment.last_error = exc.detail[:1024]
            assignment.last_checked_at = dt.now(UTC)
        await db.commit()
        return exc.detail

    present = {rule["ruleTag"] for rule in live}
    wanted: list[dict] = []
    for assignment in assignments:
        wanted.extend(assignment_rules(assignment))
    missing = [rule["ruleTag"] for rule in wanted if rule["ruleTag"] not in present]
    if not missing:
        for assignment in assignments:
            assignment.enforced = True
            assignment.last_error = None
            assignment.last_checked_at = dt.now(UTC)
        await db.commit()
        return None

    try:
        await push_live(db, node_id)
    except EnforcementError as exc:
        for assignment in assignments:
            assignment.enforced = False
            assignment.last_error = exc.detail[:1024]
            assignment.last_checked_at = dt.now(UTC)
        await db.commit()
        return exc.detail

    live = await live_rules(node_id)
    present = {rule["ruleTag"] for rule in live}
    still_missing = [rule["ruleTag"] for rule in wanted if rule["ruleTag"] not in present]
    for assignment in assignments:
        assignment.enforced = not still_missing
        assignment.last_error = (
            f"rules did not appear on the node: {', '.join(still_missing)}"[:1024] if still_missing else None
        )
        assignment.last_checked_at = dt.now(UTC)
    await db.commit()
    return assignments[0].last_error


def stale_owned_tags(live: list[dict], known_ids: set[int]) -> list[str]:
    stale: list[str] = []
    for rule in live:
        tag = rule.get("ruleTag") or ""
        if not owns_tag(tag):
            continue
        assignment_id = tag_assignment_id(tag)
        if assignment_id is None or assignment_id not in known_ids:
            stale.append(tag)
    return stale
