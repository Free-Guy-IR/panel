import json
from copy import deepcopy
from datetime import UTC, datetime as dt

from PasarGuardNodeBridge import NodeAPIError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.crud.core import get_core_config_by_id
from app.db.models import Node
from app.fork.content_filter.rules import (
    ADVISORY_NOTE,
    BLOCK_OUTBOUND,
    BLOCKING_PROTOCOL,
    CLASH_REMEDY,
    DIRECT_OUTBOUND,
    DIRECT_PROTOCOL,
    RuleValueError,
    build_rules,
    conflicting_rules,
    core_outbound_tags,
    digest,
    direct_outbound_tags,
    owns_tag,
    parse_tag,
    resolve_outbound_tags,
    strict_mode_effective,
    tag_assignment_id,
    tag_identity,
)
from app.fork.models.content_filter import ContentFilterAssignment, ContentFilterSniffingOverride
from app.fork.models.node_additional_cores import node_additional_cores_association
from app.node import node_manager
from app.utils.logger import get_logger

logger = get_logger("content-filter")

UNMATCHED_MARKER = "not enough information"
PROBE_DOMAIN = "www.pornhub.com"
DEFAULT_CORE_ID = 1
WHOLE_NODE_PROBE = "*"
ROUTABLE_PROTOCOLS = frozenset({"vless", "vmess", "trojan", "shadowsocks", "socks", "http"})
NO_FILTERABLE_ENDPOINT = "this node has no endpoint whose protocol can carry destination rules"
NOTHING_TO_ENFORCE = (
    'profile "{name}" has no categories, no blocked domains and no allow list, so there is nothing to apply'
)
BLOCKS_EVERYTHING = (
    'profile "{name}" has strict mode on and an empty allow list. Strict mode blocks everything the profile does '
    "not explicitly allow, so applying it would cut every connection on the endpoints you selected, not just the "
    "traffic you meant to filter. Put the destinations that must keep working in the allow list, or turn strict "
    "mode off and let the blocked categories do the filtering."
)
_UNREACHABLE_CORE = (
    "the endpoints it serves from core {core} are filtered only by that core's own config, which takes effect "
    "when the core reloads; the panel can install live rules into a node's main core only"
)


class EnforcementError(Exception):
    def __init__(self, detail: str, code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.code = code


class ReloadRequired(EnforcementError):
    def __init__(self, detail: str, node_ids: list[int], inbound_tags: list[str]):
        super().__init__(detail, code=409)
        self.node_ids = node_ids
        self.inbound_tags = inbound_tags


def nothing_to_enforce(assignment: ContentFilterAssignment) -> str | None:
    try:
        built = assignment_rules(assignment, [assignment.inbound_tag or WHOLE_NODE_PROBE])
        name = assignment.profile.name
    except Exception as exc:
        logger.debug(f"assignment {assignment.id}: cannot tell whether its profile is empty: {exc!r}")
        return None
    return None if built else NOTHING_TO_ENFORCE.format(name=name)


def blocks_everything(assignment: ContentFilterAssignment) -> str | None:
    try:
        profile = assignment.profile
        if not profile.strict_mode or list(profile.allow_list or []):
            return None
        if not assignment_rules(assignment, [assignment.inbound_tag or WHOLE_NODE_PROBE]):
            return None
        name = profile.name
    except Exception as exc:
        logger.debug(f"assignment {assignment.id}: cannot tell whether it blocks everything: {exc!r}")
        return None
    return BLOCKS_EVERYTHING.format(name=name)


def assignment_rules(assignment: ContentFilterAssignment, inbound_tags: list[str] | None = None) -> list[dict]:
    profile = assignment.profile
    if assignment.inbound_tag:
        tags = [assignment.inbound_tag]
    else:
        tags = list(inbound_tags or [])
        if not tags:
            return []
    try:
        return build_rules(
            assignment_id=assignment.id,
            inbound_tags=tags,
            categories=list(profile.categories or []),
            allow_list=list(profile.allow_list or []),
            block_list=list(profile.block_list or []),
            strict_mode=bool(profile.strict_mode),
        )
    except RuleValueError as exc:
        raise EnforcementError(f"assignment {assignment.id}: {exc}", code=422) from exc


def _is_strict(rule: dict) -> bool:
    parsed = parse_tag(str(rule.get("ruleTag") or ""))
    return parsed is not None and parsed[1] == "strict"


def outbound_protocol(config: dict, tag: str) -> str:
    for entry in config.get("outbounds") or []:
        if isinstance(entry, dict) and str(entry.get("tag") or "").strip() == tag:
            return str(entry.get("protocol") or "").strip().lower()
    return ""


def resolved_rules(config: dict, rules: list[dict], where: str) -> list[dict]:
    if not rules or not config:
        return rules
    if any(_is_strict(rule) for rule in rules) and not strict_mode_effective(config):
        raise EnforcementError(
            f"{where} cannot apply strict mode to hostname traffic: with routing.domainStrategy AsIs or unset, a "
            "connection made to a name never gets an address for the strict-mode catch-all to match, so that "
            "traffic is allowed instead of refused. Set routing.domainStrategy to IPIfNonMatch on that core, or "
            "turn strict mode off in this profile.",
            code=409,
        )
    needed = {str(rule.get("outboundTag") or "") for rule in rules}
    resolved = resolve_outbound_tags(config)
    keeps_destination = direct_outbound_tags(config)
    mapping: dict[str, str] = {}
    unresolved: list[str] = []

    if BLOCK_OUTBOUND in needed and outbound_protocol(config, BLOCK_OUTBOUND) != BLOCKING_PROTOCOL:
        replacement = resolved.get("block")
        if replacement is None:
            unresolved.append(f"{BLOCK_OUTBOUND} ({BLOCKING_PROTOCOL})")
        else:
            mapping[BLOCK_OUTBOUND] = replacement

    if DIRECT_OUTBOUND in needed and DIRECT_OUTBOUND not in keeps_destination:
        replacement = resolved.get("direct")
        if replacement is None:
            unresolved.append(f"{DIRECT_OUTBOUND} ({DIRECT_PROTOCOL} that keeps the destination)")
        else:
            mapping[DIRECT_OUTBOUND] = replacement

    if unresolved:
        raise EnforcementError(
            f"{where} has no {' and no '.join(sorted(unresolved))} outbound for the filter to use; "
            f"its outbounds are {', '.join(sorted(core_outbound_tags(config))) or 'none'}",
            code=409,
        )
    if not mapping:
        return rules
    return [{**rule, "outboundTag": mapping.get(rule.get("outboundTag"), rule.get("outboundTag"))} for rule in rules]


def core_id_of(node: Node) -> int:
    return DEFAULT_CORE_ID if node.core_config_id is None else node.core_config_id


async def node_core_ids(db: AsyncSession, node: Node) -> list[int]:
    core_ids: list[int] = [core_id_of(node)]
    attached = (
        (
            await db.execute(
                select(node_additional_cores_association.c.core_config_id).where(
                    node_additional_cores_association.c.node_id == node.id
                )
            )
        )
        .scalars()
        .all()
    )
    for core_id in attached:
        if core_id is not None and core_id not in core_ids:
            core_ids.append(core_id)
    return core_ids


async def core_config(db: AsyncSession, core_id: int) -> dict:
    core = await get_core_config_by_id(db, core_id)
    if core is None:
        return {}
    return core.config if isinstance(core.config, dict) else json.loads(core.config or "{}")


async def core_is_xray(db: AsyncSession, core_id: int) -> bool:
    core = await get_core_config_by_id(db, core_id)
    return core is not None and str(getattr(core.type, "value", core.type) or "") == "xray"


async def core_inbounds(db: AsyncSession, core_id: int) -> list[dict]:
    return [inbound for inbound in (await core_config(db, core_id)).get("inbounds") or [] if inbound.get("tag")]


def _filterable(inbound: dict) -> bool:
    return str(inbound.get("protocol") or "").lower() in ROUTABLE_PROTOCOLS


async def core_filterable_tags(db: AsyncSession, core_id: int) -> list[str]:
    return [str(inbound.get("tag")) for inbound in await core_inbounds(db, core_id) if _filterable(inbound)]


async def core_inbound_tags(db: AsyncSession, core_id: int) -> set[str]:
    return {str(inbound.get("tag") or "") for inbound in await core_inbounds(db, core_id)}


async def node_inbound_tags(db: AsyncSession, node_id: int) -> set[str]:
    node = await db.get(Node, node_id)
    if node is None:
        return set()
    tags: set[str] = set()
    for core_id in await node_core_ids(db, node):
        tags.update(await core_inbound_tags(db, core_id))
    return tags


def _collect_advisories(where: str, notices: list[str], into: list[str] | None) -> None:
    for notice in notices:
        logger.warning(f"{where}: {notice}")
        if into is not None and notice not in into:
            into.append(notice)


def _dedup(rules: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for rule in rules:
        tag = rule.get("ruleTag") or ""
        if tag in seen:
            continue
        seen.add(tag)
        out.append(rule)
    return out


async def rules_in_core(db: AsyncSession, assignment: ContentFilterAssignment, core_id: int) -> list[dict]:
    if not await core_is_xray(db, core_id):
        return []
    if assignment.inbound_tag:
        if assignment.inbound_tag not in await core_inbound_tags(db, core_id):
            return []
        return assignment_rules(assignment)
    if assignment.node_id is None:
        return []
    node = await db.get(Node, assignment.node_id)
    if node is None or core_id not in await node_core_ids(db, node):
        return []
    return assignment_rules(assignment, await core_filterable_tags(db, core_id))


async def node_core_rules(db: AsyncSession, assignment: ContentFilterAssignment, node_id: int) -> dict[int, list[dict]]:
    node = await db.get(Node, node_id)
    if node is None:
        return {}
    out: dict[int, list[dict]] = {}
    for core_id in await node_core_ids(db, node):
        rules = await rules_in_core(db, assignment, core_id)
        if rules:
            out[core_id] = rules
    return out


async def rules_for_node(db: AsyncSession, assignment: ContentFilterAssignment, node_id: int) -> list[dict]:
    mapping = await node_core_rules(db, assignment, node_id)
    return _dedup([rule for core_rules in mapping.values() for rule in core_rules])


async def unreachable_cores(db: AsyncSession, assignment: ContentFilterAssignment, node_id: int) -> list[int]:
    node = await db.get(Node, node_id)
    if node is None:
        return []
    live_core = core_id_of(node)
    return sorted(core_id for core_id in await node_core_rules(db, assignment, node_id) if core_id != live_core)


async def node_core_config(db: AsyncSession, node_id: int) -> dict:
    node = await db.get(Node, node_id)
    return {} if node is None else await core_config(db, core_id_of(node))


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


def _reachable(node: Node) -> bool:
    return str(getattr(node.status, "value", node.status) or "") != "disabled"


async def nodes_for_assignment(db: AsyncSession, assignment: ContentFilterAssignment) -> list[int]:
    if assignment.node_id is not None:
        node = await db.get(Node, assignment.node_id)
        return [assignment.node_id] if node is not None and _reachable(node) else []
    nodes = (await db.execute(select(Node))).scalars().all()
    reach: list[int] = []
    for node in nodes:
        if not _reachable(node):
            continue
        if assignment.inbound_tag in await node_inbound_tags(db, node.id):
            reach.append(node.id)
    return reach


async def _nodes_on_core(db: AsyncSession, core_id: int) -> list[Node]:
    attached = select(node_additional_cores_association.c.node_id).where(
        node_additional_cores_association.c.core_config_id == core_id
    )
    primary = Node.core_config_id == core_id
    if core_id == DEFAULT_CORE_ID:
        primary = or_(primary, Node.core_config_id.is_(None))
    result = await db.execute(select(Node).where(or_(primary, Node.id.in_(attached))).order_by(Node.id))
    return list(result.scalars().all())


async def _core_delivered_assignments(db: AsyncSession, core_id: int) -> list[ContentFilterAssignment]:
    delivered: dict[int, ContentFilterAssignment] = {}
    for node in await _nodes_on_core(db, core_id):
        for assignment in await _assignments_for_node(db, node.id):
            delivered.setdefault(assignment.id, assignment)
    return [delivered[key] for key in sorted(delivered)]


async def _core_persisted_assignments(db: AsyncSession, core_id: int) -> list[ContentFilterAssignment]:
    nodes = await _nodes_on_core(db, core_id)
    if not nodes:
        return []
    shared: dict[int, ContentFilterAssignment] = {}
    for index, node in enumerate(nodes):
        reaching = {row.id: row for row in await _assignments_for_node(db, node.id)}
        shared = reaching if index == 0 else {key: value for key, value in shared.items() if key in reaching}
        if not shared:
            return []
    return [shared[key] for key in sorted(shared)]


async def _rules_of(
    db: AsyncSession,
    core_id: int,
    assignments: list[ContentFilterAssignment],
    broken: list[tuple[ContentFilterAssignment, str]] | None = None,
) -> list[dict]:
    rules: list[dict] = []
    seen: set[str] = set()
    for assignment in assignments:
        try:
            built = await rules_in_core(db, assignment, core_id)
        except EnforcementError as exc:
            logger.warning(f"core {core_id}: assignment {assignment.id} left out of the config: {exc.detail}")
            if broken is not None and all(row is not assignment for row, _ in broken):
                broken.append((assignment, exc.detail))
            continue
        for rule in built:
            tag = rule.get("ruleTag") or ""
            if tag in seen:
                continue
            seen.add(tag)
            rules.append(rule)
    return rules


async def core_persisted_rules(
    db: AsyncSession, core_id: int, broken: list[tuple[ContentFilterAssignment, str]] | None = None
) -> list[dict]:
    return await _rules_of(db, core_id, await _core_persisted_assignments(db, core_id), broken)


async def core_delivered_rules(
    db: AsyncSession, core_id: int, broken: list[tuple[ContentFilterAssignment, str]] | None = None
) -> list[dict]:
    return await _rules_of(db, core_id, await _core_delivered_assignments(db, core_id), broken)


async def _reaches(db: AsyncSession, assignment: ContentFilterAssignment, node_id: int) -> bool:
    return any(row.id == assignment.id for row in await _assignments_for_node(db, node_id))


async def _nodes_reached(db: AsyncSession, assignment: ContentFilterAssignment) -> list[Node]:
    if assignment.node_id is not None:
        node = await db.get(Node, assignment.node_id)
        if node is None or not await _reaches(db, assignment, node.id):
            return []
        return [node]
    nodes = (await db.execute(select(Node))).scalars().all()
    return [node for node in nodes if await _reaches(db, assignment, node.id)]


async def assignment_delivery(db: AsyncSession, assignment: ContentFilterAssignment) -> str:
    nodes = await _nodes_reached(db, assignment)
    if not nodes:
        return "live"
    for node in nodes:
        carrying: list[int] = []
        for core_id in await node_core_ids(db, node):
            try:
                built = await rules_in_core(db, assignment, core_id)
            except EnforcementError as exc:
                logger.warning(f"core {core_id}: assignment {assignment.id} builds no rules: {exc.detail}")
                continue
            if built:
                carrying.append(core_id)
        if not carrying:
            return "live"
        for core_id in carrying:
            if assignment.id not in {row.id for row in await _core_persisted_assignments(db, core_id)}:
                return "live"
    return "core"


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
    routing["rules"] = _strip_owned(current) + filter_rules

    return config


def wanted_sniffing_tags(config: dict, filter_rules: list[dict]) -> set[str]:
    tags = {str(i.get("tag") or "") for i in config.get("inbounds") or [] if i.get("tag")}
    tags.discard("API_INBOUND")
    return tags & _scoped_tags(filter_rules)


LEDGER_OWNER = "content-filter"


def _sniffing_is_enough(current) -> bool:
    if not isinstance(current, dict) or not current.get("enabled"):
        return False
    if current.get("metadataOnly"):
        return False
    have = {str(name).lower() for name in current.get("destOverride") or []}
    return set(SNIFFING["destOverride"]).issubset(have)


def _sniffing_exclusions(current) -> list[str]:
    if not isinstance(current, dict):
        return []
    return sorted({str(value) for value in current.get("domainsExcluded") or [] if str(value).strip()})


def _ledger_entry(previous, written: dict) -> dict:
    return {"owner": LEDGER_OWNER, "previous": previous, "written": written}


def _ledger_parts(row: ContentFilterSniffingOverride):
    payload = row.original
    if isinstance(payload, dict) and payload.get("owner") == LEDGER_OWNER:
        return payload.get("previous"), payload.get("written")
    return payload, None


class SniffingPlan:
    def __init__(self):
        self.add: list[ContentFilterSniffingOverride] = []
        self.update: list[tuple[ContentFilterSniffingOverride, dict]] = []
        self.delete: list[ContentFilterSniffingOverride] = []
        self.enabled: list[str] = []
        self.restored: list[str] = []
        self.problems: list[str] = []


async def plan_sniffing_overrides(db: AsyncSession, core_id: int, config: dict, wanted: set[str]) -> SniffingPlan:
    rows = (
        (
            await db.execute(
                select(ContentFilterSniffingOverride).where(ContentFilterSniffingOverride.core_id == core_id)
            )
        )
        .scalars()
        .all()
    )
    owned = {row.inbound_tag: row for row in rows}
    by_tag = {str(i.get("tag") or ""): i for i in config.get("inbounds") or []}
    plan = SniffingPlan()

    for tag in sorted(wanted):
        inbound = by_tag.get(tag)
        if inbound is None:
            continue
        current = inbound.get("sniffing")
        row = owned.get(tag)
        excluded = _sniffing_exclusions(current)
        if _sniffing_is_enough(current):
            if excluded:
                plan.problems.append(
                    f"endpoint {tag}: name recovery is switched off for {', '.join(excluded[:5])}, and whether that "
                    "covers a hostname this filter blocks cannot be decided here; narrow that list if the filter "
                    "must decide the traffic"
                )
            continue
        if row is None:
            plan.add.append(
                ContentFilterSniffingOverride(
                    core_id=core_id, inbound_tag=tag, original=_ledger_entry(current, dict(SNIFFING))
                )
            )
        else:
            previous, written = _ledger_parts(row)
            ours = written if written is not None else dict(SNIFFING)
            if current != ours and current != previous:
                plan.update.append((row, _ledger_entry(current, dict(SNIFFING))))
                plan.problems.append(
                    f"endpoint {tag}: sniffing was changed outside the filter; that value is now what withdrawal restores"
                )
        if excluded:
            plan.problems.append(
                f"endpoint {tag}: name recovery replaced its exclusion list ({', '.join(excluded[:5])}); "
                "withdrawing the filter puts that list back"
            )
        if current != SNIFFING:
            plan.enabled.append(tag)
        inbound["sniffing"] = dict(SNIFFING)

    for tag, row in owned.items():
        if tag in wanted:
            continue
        inbound = by_tag.get(tag)
        previous, written = _ledger_parts(row)
        if inbound is None:
            plan.problems.append(
                f"endpoint {tag} is no longer in core {core_id}'s config while the filter still holds its original "
                "sniffing setting; the record is kept so it can be restored by hand"
            )
            continue
        current = inbound.get("sniffing")
        ours = written if written is not None else dict(SNIFFING)
        if current != ours:
            plan.problems.append(f"endpoint {tag}: sniffing was changed outside the filter and was left untouched")
            plan.delete.append(row)
            continue
        if previous is None:
            inbound.pop("sniffing", None)
        else:
            inbound["sniffing"] = previous
        plan.restored.append(tag)
        plan.delete.append(row)

    return plan


async def _commit_sniffing_plan(db: AsyncSession, plan: SniffingPlan) -> None:
    for row in plan.add:
        db.add(row)
    for row, payload in plan.update:
        row.original = payload
    for row in plan.delete:
        await db.delete(row)
    await db.flush()


async def persist_core_rules(
    db: AsyncSession, core_id: int, admin, allow_restart: bool = False, advisories: list[str] | None = None
) -> bool:
    from app.models.core import CoreCreate
    from app.operation import OperatorType
    from app.operation.core import CoreOperation

    db_core = await get_core_config_by_id(db, core_id)
    if db_core is None:
        raise EnforcementError(f"core {core_id} not found", code=404)

    broken: list[tuple[ContentFilterAssignment, str]] = []
    filter_rules = await core_persisted_rules(db, core_id, broken)
    delivered_rules = await core_delivered_rules(db, core_id, broken)
    for assignment, detail in broken:
        await _record(db, assignment, False, [f"core {core_id}: {detail}"])
    current = db_core.config if isinstance(db_core.config, dict) else json.loads(db_core.config or "{}")
    filter_rules = resolved_rules(current, filter_rules, f"core {core_id}")
    if filter_rules:
        existing_rules = _strip_owned((current.get("routing") or {}).get("rules") or [])
        scope_tags = sorted(_scoped_tags(filter_rules))
        overlap = conflicting_rules(existing_rules, scope_tags, current, filter_rules)
        if overlap.clashes:
            raise EnforcementError(
                f"core {core_id} already routes this traffic before the filter would see it: "
                + ", ".join(overlap.clashes[:5])
                + f". {CLASH_REMEDY}",
                code=409,
            )
        _collect_advisories(f"core {core_id}", overlap.advisories, advisories)
    updated = build_core_config(current, filter_rules)
    plan = await plan_sniffing_overrides(db, core_id, updated, wanted_sniffing_tags(updated, delivered_rules))
    _collect_advisories(f"core {core_id}", plan.problems, advisories)
    if updated == current:
        await _commit_sniffing_plan(db, plan)
        return False

    reload_tags = sorted(set(plan.enabled) | set(plan.restored))
    if reload_tags and not allow_restart:
        nodes = [node.id for node in await _nodes_on_core(db, core_id) if _reachable(node)]
        raise ReloadRequired(
            f"core {core_id} needs name recovery changed on {reload_tags}, which only takes effect after a reload: "
            f"nodes {nodes} would restart and drop their current sessions",
            node_ids=nodes,
            inbound_tags=reload_tags,
        )

    await _commit_sniffing_plan(db, plan)
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
    if reload_tags:
        logger.info(f"core {core_id}: name recovery changed on {reload_tags}; restarting its nodes")
        await _restart_core_nodes(db, core_id, admin)
    problems = await refresh_core_nodes(db, core_id)
    if problems:
        logger.warning(f"core {core_id}: live rules still stale on some of its nodes: {'; '.join(problems)}")
    return True


async def refresh_core_nodes(db: AsyncSession, core_id: int) -> list[str]:
    problems: list[str] = []
    for node in await _nodes_on_core(db, core_id):
        if not _reachable(node):
            continue
        try:
            await push_live(db, node.id)
        except EnforcementError as exc:
            problems.append(f"node {node.id}: {exc.detail}")
        except Exception as exc:
            problems.append(f"node {node.id}: {exc!r}")
    return problems


async def _restart_core_nodes(db: AsyncSession, core_id: int, admin) -> None:
    from app.operation import OperatorType
    from app.operation.node import NodeOperation

    operator = NodeOperation(operator_type=OperatorType.SYSTEM)
    try:
        await operator.restart_all_node(db=db, core_id=core_id, admin=admin)
    except Exception as exc:
        logger.warning(f"core {core_id}: could not restart its nodes after enabling name recovery: {exc}")


async def _main_core_routing(node_id: int) -> tuple[int | None, bool | None]:
    from app.db import GetDB

    try:
        async with GetDB() as db:
            node = await db.get(Node, node_id)
            if node is None:
                return None, None
            core_id = core_id_of(node)
            core = await get_core_config_by_id(db, core_id)
            if core is None:
                return core_id, None
            config = core.config if isinstance(core.config, dict) else json.loads(core.config or "{}")
            services = (config.get("api") or {}).get("services") or []
            return core_id, any(str(name) == "RoutingService" for name in services)
    except Exception as exc:
        logger.warning(f"node {node_id}: could not read its stored core config: {exc!r}")
        return None, None


async def routing_service_declared(node_id: int) -> bool | None:
    return (await _main_core_routing(node_id))[1]


async def _routing_service_detail(node_id: int) -> str:
    core_id, declared = await _main_core_routing(node_id)
    where = f"core {core_id}" if core_id is not None else "the core config stored for it"
    if declared is True:
        return (
            f"node {node_id} answers that it is not serving RoutingService, but {where}, the core it runs, does "
            "list RoutingService. Its running core is either stale or still starting, so reconnect or restart the "
            "node rather than changing the config."
        )
    if declared is False:
        return (
            f"node {node_id} answers that it is not serving RoutingService, and {where}, the core it runs, does not "
            f'list it. Add "RoutingService" to api.services in {where} and restart the node.'
        )
    return (
        f"node {node_id} answers that it is not serving RoutingService. Check whether {where} lists RoutingService "
        "under api.services, and if it does, reconnect the node so it picks it up."
    )


async def live_rules(node_id: int) -> list[dict]:
    node = await node_manager.get_node(node_id)
    if node is None:
        raise EnforcementError(f"node {node_id} is not attached to this panel", code=404)
    try:
        response = await node.list_routing_rules()
    except NodeAPIError as exc:
        if exc.code == 501:
            raise EnforcementError(await _routing_service_detail(node_id), code=409) from exc
        raise EnforcementError(exc.detail or str(exc), code=exc.code if exc.code > 0 else 503) from exc
    return [{"ruleTag": rule.rule_tag or "", "outboundTag": rule.outbound_tag or ""} for rule in (response.rules or [])]


async def _remove_owned(node, tags: list[str]) -> None:
    for tag in tags:
        try:
            await node.remove_routing_rule(rule_tag=tag)
        except NodeAPIError as exc:
            logger.warning(f"could not remove routing rule {tag}: {exc.detail}")


async def push_live(db: AsyncSession, node_id: int, advisories: list[str] | None = None) -> list[dict]:
    node = await node_manager.get_node(node_id)
    if node is None:
        raise EnforcementError(f"node {node_id} is not attached to this panel", code=404)

    row = await db.get(Node, node_id)
    live_core = None if row is None else core_id_of(row)
    config = await node_core_config(db, node_id)
    wanted: list[dict] = []
    for assignment in await _assignments_for_node(db, node_id):
        try:
            mapping = await node_core_rules(db, assignment, node_id)
        except EnforcementError as exc:
            logger.warning(f"node {node_id}: assignment {assignment.id} left out of this push: {exc.detail}")
            await _record(db, assignment, False, [f"node {node_id}: {exc.detail}"])
            continue
        wanted.extend(resolved_rules(config, mapping.get(live_core) or [], f"core {live_core} on node {node_id}"))
    wanted = _dedup(wanted)
    existing = await live_rules(node_id)

    if wanted:
        remaining = [rule for rule in existing if not owns_tag(rule["ruleTag"])]
        scope = {tag for rule in wanted for tag in rule.get("inboundTag", [])}
        overlap = conflicting_rules(remaining, sorted(scope), config or None, wanted)
        if overlap.clashes:
            raise EnforcementError(
                f"node {node_id} already routes this traffic before the filter would see it: "
                + ", ".join(overlap.clashes[:5])
                + f". {CLASH_REMEDY}",
                code=409,
            )
        _collect_advisories(f"node {node_id}", overlap.advisories, advisories)

    installed = [rule for rule in existing if owns_tag(rule["ruleTag"])]
    by_tag = {rule["ruleTag"]: rule for rule in wanted}
    rerouted = [
        rule["ruleTag"]
        for rule in installed
        if rule["ruleTag"] in by_tag
        and (rule.get("outboundTag") or "") != (by_tag[rule["ruleTag"]].get("outboundTag") or "")
    ]
    if [rule["ruleTag"] for rule in installed] == [rule["ruleTag"] for rule in wanted] and not rerouted:
        return wanted

    await _remove_owned(node, [rule["ruleTag"] for rule in installed])
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
        result = await node.test_route(inbound_tag=inbound_tag, network="tcp", target_domain=domain, target_port=443)
    except NodeAPIError as exc:
        if UNMATCHED_MARKER in (exc.detail or ""):
            return "unmatched"
        return f"error:{exc.detail}"
    return result.outbound_tag if result else "unmatched"


async def _cores_of_nodes(db: AsyncSession, node_ids: list[int]) -> list[int]:
    cores: list[int] = []
    for node_id in node_ids:
        node = await db.get(Node, node_id)
        if node is None:
            continue
        for core_id in await node_core_ids(db, node):
            if core_id not in cores:
                cores.append(core_id)
    return cores


async def cores_of_assignment(db: AsyncSession, assignment: ContentFilterAssignment) -> list[int]:
    if assignment.node_id is not None:
        node = await db.get(Node, assignment.node_id)
        return [] if node is None else list(await node_core_ids(db, node))
    cores: list[int] = []
    for node in (await db.execute(select(Node))).scalars().all():
        if assignment.inbound_tag not in await node_inbound_tags(db, node.id):
            continue
        for core_id in await node_core_ids(db, node):
            if core_id not in cores:
                cores.append(core_id)
    return cores


async def _rebuild_cores_by_id(
    db: AsyncSession, core_ids: list[int], admin, allow_restart: bool, advisories: list[str] | None = None
) -> list[str]:
    problems: list[str] = []
    for core_id in core_ids:
        try:
            await persist_core_rules(db, core_id, admin, allow_restart=allow_restart, advisories=advisories)
        except ReloadRequired:
            raise
        except EnforcementError as exc:
            problems.append(f"core {core_id}: {exc.detail}")
        except Exception as exc:
            problems.append(f"core {core_id}: {exc!r}")
    return problems


async def _rebuild_cores(
    db: AsyncSession, node_ids: list[int], admin, allow_restart: bool, advisories: list[str] | None = None
) -> list[str]:
    return await _rebuild_cores_by_id(db, await _cores_of_nodes(db, node_ids), admin, allow_restart, advisories)


async def _record(db: AsyncSession, assignment: ContentFilterAssignment, enforced: bool, problems: list[str]) -> None:
    assignment.enforced = enforced
    assignment.last_error = "; ".join(problems)[:1024] if problems else None
    assignment.last_checked_at = dt.now(UTC)
    await db.commit()


async def apply_assignment(
    db: AsyncSession, assignment: ContentFilterAssignment, admin, allow_restart: bool = False
) -> dict:
    for refusal in (nothing_to_enforce(assignment), blocks_everything(assignment)):
        if refusal is not None:
            await _record(db, assignment, False, [refusal])
            raise EnforcementError(refusal, code=409)

    node_ids = await nodes_for_assignment(db, assignment)
    if not node_ids:
        await _record(db, assignment, False, ["no node currently carries that endpoint"])
        raise EnforcementError("no node currently carries that endpoint", code=404)

    reachable: list[int] = []
    problems: list[str] = []
    per_node: dict[int, list[dict]] = {}
    for node_id in node_ids:
        try:
            await live_rules(node_id)
        except EnforcementError as exc:
            problems.append(f"node {node_id}: {exc.detail}")
            continue
        try:
            mapping = await node_core_rules(db, assignment, node_id)
        except EnforcementError as exc:
            await _record(db, assignment, False, [exc.detail])
            raise
        if not any(mapping.values()):
            problems.append(f"node {node_id}: {NO_FILTERABLE_ENDPOINT}")
            continue
        row = await db.get(Node, node_id)
        live_core = None if row is None else core_id_of(row)
        for core_id in sorted(core_id for core_id in mapping if core_id != live_core):
            problems.append(f"node {node_id}: {_UNREACHABLE_CORE.format(core=core_id)}")
        per_node[node_id] = mapping.get(live_core) or []
        reachable.append(node_id)

    if not reachable:
        await _record(db, assignment, False, problems or ["no node can accept routing rules"])
        raise EnforcementError(assignment.last_error or "no node can accept routing rules", code=409)

    advisories: list[str] = []
    problems.extend(await _rebuild_cores(db, reachable, admin, allow_restart, advisories))

    assignment.applied_digest = digest(per_node[reachable[0]])
    reached = 0
    for node_id in reachable:
        try:
            await push_live(db, node_id, advisories)
        except EnforcementError as exc:
            problems.append(f"node {node_id}: {exc.detail}")
            continue
        present = {rule["ruleTag"] for rule in await live_rules(node_id)}
        missing = [rule["ruleTag"] for rule in per_node[node_id] if rule["ruleTag"] not in present]
        if missing:
            problems.append(f"node {node_id}: rules did not appear ({', '.join(missing)})")
        else:
            reached += 1

    await _record(db, assignment, reached == len(node_ids) and not problems, problems)

    if not assignment.enforced:
        delivery = await assignment_delivery(db, assignment)
        raise EnforcementError(
            f"in force on {reached} of {len(node_ids)} nodes, delivery {delivery}: "
            f"{assignment.last_error or 'could not confirm enforcement'}",
            code=502,
        )

    probe_tag = assignment.inbound_tag or next(
        iter(tag for rule in per_node[reachable[0]] for tag in rule.get("inboundTag") or []), ""
    )
    verdict = await probe(reachable[0], probe_tag, PROBE_DOMAIN)
    return {
        "rules": len(per_node[reachable[0]]),
        "nodes": reached,
        "probe": verdict,
        "advisories": advisories,
        "advisory_note": ADVISORY_NOTE if advisories else "",
    }


async def withdraw_assignment(
    db: AsyncSession, assignment: ContentFilterAssignment, admin, allow_restart: bool = False
) -> None:
    node_ids = await nodes_for_assignment(db, assignment)
    core_ids = await cores_of_assignment(db, assignment)
    assignment.is_enabled = False
    await db.commit()

    problems = await _rebuild_cores_by_id(db, core_ids, admin, allow_restart)
    for node_id in node_ids:
        try:
            await push_live(db, node_id)
        except EnforcementError as exc:
            problems.append(f"node {node_id}: {exc.detail}")
        except Exception as exc:
            problems.append(f"node {node_id}: {exc!r}")

    if problems:
        await _record(db, assignment, False, problems)
        raise EnforcementError(
            f"the filter is switched off but could not be removed everywhere, so the assignment was kept: "
            f"{assignment.last_error}",
            code=502,
        )

    await db.delete(assignment)
    await db.commit()


async def _node_problems(db: AsyncSession, assignment: ContentFilterAssignment, node_id: int) -> list[str]:
    try:
        wanted = await rules_for_node(db, assignment, node_id)
        unreachable = await unreachable_cores(db, assignment, node_id)
    except EnforcementError as exc:
        return [f"node {node_id}: {exc.detail}"]
    if not wanted:
        empty = nothing_to_enforce(assignment)
        return [empty] if empty is not None else [f"node {node_id}: {NO_FILTERABLE_ENDPOINT}"]
    problems = [f"node {node_id}: {_UNREACHABLE_CORE.format(core=core_id)}" for core_id in unreachable]
    try:
        live = await live_rules(node_id)
    except EnforcementError as exc:
        return [*problems, f"node {node_id}: {exc.detail}"]
    present = {rule["ruleTag"] for rule in live}
    missing = [rule["ruleTag"] for rule in wanted if rule["ruleTag"] not in present]
    if missing:
        problems.append(f"node {node_id}: rules did not appear ({', '.join(missing)})")
    return problems


async def _fleet_problems(db: AsyncSession, assignment: ContentFilterAssignment) -> list[str]:
    nodes = await _nodes_reached(db, assignment)
    if not nodes:
        return ["no node currently carries that endpoint"]
    problems: list[str] = []
    for node in nodes:
        problems.extend(await _node_problems(db, assignment, node.id))
    return problems


async def reconcile_node(db: AsyncSession, node_id: int) -> str | None:
    assignments = await _assignments_for_node(db, node_id)
    if not assignments:
        return None

    try:
        live = await live_rules(node_id)
    except EnforcementError as exc:
        for assignment in assignments:
            await _record(db, assignment, False, [f"node {node_id}: {exc.detail}"])
        return exc.detail

    present = {rule["ruleTag"] for rule in live}
    wanted: list[dict] = []
    for assignment in assignments:
        try:
            wanted.extend(await rules_for_node(db, assignment, node_id))
        except EnforcementError as exc:
            await _record(db, assignment, False, [f"node {node_id}: {exc.detail}"])
    missing = [rule["ruleTag"] for rule in wanted if rule["ruleTag"] not in present]

    if missing:
        try:
            await push_live(db, node_id)
        except EnforcementError as exc:
            for assignment in assignments:
                await _record(db, assignment, False, [f"node {node_id}: {exc.detail}"])
            return exc.detail

    first_error: str | None = None
    for assignment in assignments:
        problems = (
            await _node_problems(db, assignment, node_id)
            if assignment.node_id is not None
            else await _fleet_problems(db, assignment)
        )
        await _record(db, assignment, not problems, problems)
        if problems and first_error is None:
            first_error = assignment.last_error
    return first_error


def stale_owned_tags(live: list[dict], known_ids: set[int], wanted_tags: set[str] | None = None) -> list[str]:
    stale: list[str] = []
    for rule in live:
        tag = rule.get("ruleTag") or ""
        if not owns_tag(tag):
            continue
        if wanted_tags is not None:
            if tag not in wanted_tags:
                stale.append(tag)
            continue
        if tag_identity(tag) is None:
            stale.append(tag)
            continue
        assignment_id = tag_assignment_id(tag)
        if assignment_id is None or assignment_id not in known_ids:
            stale.append(tag)
    return stale
