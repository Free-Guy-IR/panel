import json
from datetime import UTC, datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.crud.core import get_core_config_by_id
from app.db.models import Node
from app.fork.content_filter import capability, service
from app.fork.content_filter.catalog import catalog_payload
from app.fork.content_filter.rules import RuleValueError
from app.fork.content_filter.schemas import (
    AssignmentBulkPayload,
    AssignmentBulkResult,
    AssignmentOutcome,
    AssignmentPayload,
    AssignmentResponse,
    CapabilityReport,
    CatalogResponse,
    DestinationTest,
    DestinationVerdict,
    ProfilePayload,
    ProfileResponse,
    ReloadPrompt,
    TargetInbound,
    TargetNode,
)
from app.fork.content_filter.service import EnforcementError, ReloadRequired
from app.fork.models.content_filter import ContentFilterAssignment, ContentFilterProfile
from app.models.admin import AdminDetails
from app.routers.authentication import require_permission
from app.utils.logger import get_logger

router = APIRouter(tags=["Content Filter"], prefix="/api/content-filter")

OWNER_MESSAGE = "only an admin with full panel access can use the content filter"


def _owner_gate(resource: str, action: str):
    checked = require_permission(resource, action)

    async def dependency(admin: AdminDetails = Depends(checked)) -> AdminDetails:
        if not admin.is_owner:
            raise HTTPException(status_code=403, detail=OWNER_MESSAGE)
        return admin

    return dependency


OWNER_READ = _owner_gate("settings", "read")
OWNER_WRITE = _owner_gate("settings", "update")
logger = get_logger("content-filter-api")

ROUTABLE_PROTOCOLS = service.ROUTABLE_PROTOCOLS
UNROUTABLE_REASON = "this protocol has no domain routing engine"
NON_XRAY_REASON = "only xray cores can enforce destination rules"
BAD_PROFILE = "this filter profile holds a value that cannot be turned into a rule: {reason}"


def _fail(exc: EnforcementError):
    raise HTTPException(status_code=exc.code, detail=exc.detail)


def _reload_prompt(exc: ReloadRequired) -> ReloadPrompt:
    return ReloadPrompt(message=exc.detail, node_ids=list(exc.node_ids), inbound_tags=list(exc.inbound_tags))


def _ask_restart(exc: ReloadRequired):
    raise HTTPException(status_code=exc.code, detail=_reload_prompt(exc).model_dump()) from None


def _refuse(exc: RuleValueError):
    raise HTTPException(status_code=422, detail=BAD_PROFILE.format(reason=exc)) from None


async def _safe(coro):
    try:
        return await coro
    except RuleValueError as exc:
        _refuse(exc)


async def _readable(coro):
    try:
        return await coro
    except RuleValueError as exc:
        _refuse(exc)
    except EnforcementError as exc:
        _fail(exc)


async def _node_cores(db: AsyncSession, node: Node) -> list[tuple[int, str, dict]]:
    out: list[tuple[int, str, dict]] = []
    for core_id in await service.node_core_ids(db, node):
        core = await get_core_config_by_id(db, core_id)
        if core is None:
            continue
        config = core.config if isinstance(core.config, dict) else json.loads(core.config or "{}")
        out.append((core_id, str(getattr(core.type, "value", core.type) or ""), config))
    return out


def _xray_inbounds(cores: list[tuple[int, str, dict]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for _, core_type, config in cores:
        if core_type != "xray":
            continue
        for inbound in config.get("inbounds") or []:
            tag = str(inbound.get("tag") or "")
            if tag:
                found.setdefault(tag, str(inbound.get("protocol") or "").lower())
    return found


def _serves_routing(cores: list[tuple[int, str, dict]]) -> bool:
    for _, core_type, config in cores:
        if core_type != "xray":
            continue
        services = ((config.get("api") or {}).get("services")) or []
        if any(str(name).lower() == "routingservice" for name in services):
            return True
    return False


async def _load_profile(db: AsyncSession, profile_id: int) -> ContentFilterProfile:
    profile = await db.get(ContentFilterProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="filter profile not found")
    return profile


async def _carriers_of(db: AsyncSession, inbound_tag: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in (await db.execute(select(Node))).scalars().all():
        protocols = _xray_inbounds(await _node_cores(db, node))
        if inbound_tag in protocols:
            found.append((node.id, protocols[inbound_tag]))
    return found


async def _load_assignment(db: AsyncSession, assignment_id: int) -> ContentFilterAssignment:
    assignment = await db.get(ContentFilterAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="assignment not found")
    return assignment


async def _withdraw_rules(
    db: AsyncSession, assignment: ContentFilterAssignment, admin, confirmed: bool
) -> tuple[str | None, ReloadPrompt | None]:
    problems: list[str] = []
    prompt: ReloadPrompt | None = None
    for core_id in await service.cores_of_assignment(db, assignment):
        try:
            await service.persist_core_rules(db, core_id, admin, allow_restart=confirmed, drop_unsupported=True)
        except ReloadRequired as exc:
            prompt = prompt or _reload_prompt(exc)
            problems.append(f"core {core_id}: {exc.detail}")
        except (EnforcementError, RuleValueError) as exc:
            problems.append(f"core {core_id}: {getattr(exc, 'detail', exc)}")
    for node_id in await service.nodes_for_assignment(db, assignment):
        try:
            await service.push_live(db, node_id)
        except (EnforcementError, RuleValueError) as exc:
            problems.append(f"node {node_id}: {getattr(exc, 'detail', exc)}")
    reported = "; ".join(problems) if problems else None
    assignment.enforced = False
    assignment.last_error = reported[:1024] if reported else None
    assignment.last_checked_at = dt.now(UTC)
    await db.commit()
    return reported, prompt


def _reaches(assignment: ContentFilterAssignment, node: Node, tags: set[str]) -> bool:
    if not assignment.is_enabled:
        return False
    if assignment.node_id is not None:
        return assignment.node_id == node.id
    return assignment.inbound_tag in tags


def _built_rules(assignment: ContentFilterAssignment) -> list[dict]:
    try:
        return service.assignment_rules(assignment)
    except RuleValueError, EnforcementError:
        return []


async def _delivery_map(db: AsyncSession, assignments: list[ContentFilterAssignment]) -> dict[int, str]:
    if not assignments:
        return {}
    nodes = list((await db.execute(select(Node))).scalars().all())
    tags_of: dict[int, set[str]] = {node.id: await service.node_inbound_tags(db, node.id) for node in nodes}
    cores_of: dict[int, list[int]] = {node.id: await service.node_core_ids(db, node) for node in nodes}
    persisted: dict[int, set[int]] = {}
    for core_id in {core_id for ids in cores_of.values() for core_id in ids}:
        persisted[core_id] = await _readable(service.core_persisted_owners(db, core_id))

    out: dict[int, str] = {}
    for assignment in assignments:
        if not _built_rules(assignment):
            out[assignment.id] = await _readable(service.assignment_delivery(db, assignment))
            continue
        reached = [node for node in nodes if _reaches(assignment, node, tags_of.get(node.id, set()))]
        durable = all(
            any(assignment.id in persisted.get(core_id, set()) for core_id in cores_of.get(node.id, []))
            for node in reached
        )
        out[assignment.id] = "core" if reached and durable else "live"
    return out


async def _assignment_view(
    db: AsyncSession,
    assignment: ContentFilterAssignment,
    delivery: str | None = None,
    applied: dict | None = None,
) -> AssignmentResponse:
    notice = applied or {}
    return AssignmentResponse(
        id=assignment.id,
        profile_id=assignment.profile_id,
        node_id=assignment.node_id,
        inbound_tag=assignment.inbound_tag,
        is_enabled=assignment.is_enabled,
        enforced=assignment.enforced,
        last_checked_at=assignment.last_checked_at,
        last_error=assignment.last_error,
        delivery=delivery if delivery is not None else await _readable(service.assignment_delivery(db, assignment)),
        reaches_nodes=sorted(await service.nodes_for_assignment(db, assignment)),
        advisories=list(notice.get("advisories") or []),
        advisory_note=str(notice.get("advisory_note") or ""),
    )


@router.get("/catalog", response_model=CatalogResponse)
async def get_catalog(_: AdminDetails = Depends(OWNER_READ)):
    return catalog_payload()


@router.get("/capability", response_model=CapabilityReport)
async def get_capability(
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(OWNER_READ),
):
    return await service.capability_report(db)


def _scope_note(usable: bool, sharing: list[int]) -> str:
    peers = ", ".join(str(node_id) for node_id in sharing)
    if not usable:
        if sharing:
            return (
                "nothing can be filtered on this node right now, so a filter pinned to it reaches no node; "
                f"nodes {peers} use the same core config"
            )
        return "nothing can be filtered on this node right now, so a filter pinned to it reaches no node"
    if sharing:
        return (
            "a filter pinned to this node runs on this node only; "
            f"nodes {peers} use the same core config and would also have been filtered "
            "if the panel had written the filter into that config"
        )
    return "a filter pinned to this node runs on this node only; no other node uses its core config"


@router.get("/targets", response_model=list[TargetNode])
async def get_targets(
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(OWNER_READ),
):
    nodes = list((await db.execute(select(Node))).scalars().all())
    cores_of: dict[int, list[tuple[int, str, dict]]] = {node.id: await _node_cores(db, node) for node in nodes}
    effective: dict[int, list[int]] = {node.id: await service.node_core_ids(db, node) for node in nodes}
    members: dict[int, set[int]] = {}
    for node in nodes:
        for core_id in effective[node.id]:
            members.setdefault(core_id, set()).add(node.id)

    out: list[TargetNode] = []
    for node in nodes:
        cores = cores_of[node.id]
        is_xray = any(core_type == "xray" for _, core_type, _ in cores)
        routing = _serves_routing(cores)
        by_tag: dict[str, TargetInbound] = {}
        for _, core_type, config in cores:
            for inbound in config.get("inbounds") or []:
                tag = str(inbound.get("tag") or "")
                protocol = str(inbound.get("protocol") or "").lower()
                if not tag or tag == "API_INBOUND":
                    continue
                if core_type != "xray":
                    entry = TargetInbound(tag=tag, protocol=protocol, filterable=False, reason=NON_XRAY_REASON)
                elif protocol not in ROUTABLE_PROTOCOLS:
                    entry = TargetInbound(tag=tag, protocol=protocol, filterable=False, reason=UNROUTABLE_REASON)
                else:
                    entry = TargetInbound(tag=tag, protocol=protocol, filterable=True)
                known = by_tag.get(tag)
                if known is None or (entry.filterable and not known.filterable):
                    by_tag[tag] = entry
        status = str(getattr(node.status, "value", node.status) or "")
        if not is_xray:
            reason = NON_XRAY_REASON
        elif status == "disabled":
            reason = "this node is disabled, so the panel never connects to it and no rule can reach it"
        elif not routing:
            reason = (
                'this node\'s core does not publish RoutingService; add "api": {"services": ["RoutingService"]} '
                "to its core config and restart it. If the core already declares it, the node agent is older than "
                "0.5.4, which predates the service, and needs upgrading"
            )
        else:
            reason = None
        sharing = sorted({peer for core_id in effective[node.id] for peer in members[core_id]} - {node.id})
        out.append(
            TargetNode(
                id=node.id,
                name=node.name,
                status=status,
                core_config_id=effective[node.id][0],
                routing_service=routing,
                reason=reason,
                shares_core_with=sharing,
                pinned_stays_here=reason is None,
                would_also_affect=sharing,
                scope_note=_scope_note(reason is None, sharing),
                inbounds=list(by_tag.values()),
            )
        )
    return out


@router.get("/profiles", response_model=list[ProfileResponse])
async def list_profiles(
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(OWNER_READ),
):
    rows = (await db.execute(select(ContentFilterProfile).order_by(ContentFilterProfile.id))).scalars().all()
    return [ProfileResponse.model_validate(row) for row in rows]


@router.post("/profiles", response_model=ProfileResponse, status_code=201)
async def create_profile(
    payload: ProfilePayload,
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(OWNER_WRITE),
):
    exists = (
        await db.execute(select(ContentFilterProfile).where(ContentFilterProfile.name == payload.name))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="a profile with that name already exists")

    profile = ContentFilterProfile(
        name=payload.name,
        categories=payload.categories,
        allow_list=payload.allow_list,
        block_list=payload.block_list,
        strict_mode=payload.strict_mode,
        note=payload.note,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    return ProfileResponse.model_validate(profile)


@router.put("/profiles/{profile_id}", response_model=ProfileResponse)
async def update_profile(
    profile_id: int,
    payload: ProfilePayload,
    confirm_restart: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_WRITE),
):
    profile = await _load_profile(db, profile_id)
    clash = (
        await db.execute(
            select(ContentFilterProfile).where(
                ContentFilterProfile.name == payload.name, ContentFilterProfile.id != profile_id
            )
        )
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(status_code=409, detail="a profile with that name already exists")

    profile.name = payload.name
    profile.categories = payload.categories
    profile.allow_list = payload.allow_list
    profile.block_list = payload.block_list
    profile.strict_mode = payload.strict_mode
    profile.note = payload.note
    profile.updated_at = dt.now(UTC)
    await db.commit()
    await db.refresh(profile)

    for assignment in list(profile.assignments):
        if not assignment.is_enabled:
            continue
        try:
            await service.apply_assignment(db, assignment, admin, allow_restart=confirm_restart)
        except EnforcementError as exc:
            logger.warning(f"profile {profile_id}: re-apply on node {assignment.node_id} failed: {exc.detail}")
        except RuleValueError as exc:
            logger.warning(f"profile {profile_id}: node {assignment.node_id} kept its old rules: {exc}")
    return ProfileResponse.model_validate(profile)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: int,
    confirm_restart: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_WRITE),
):
    profile = await _load_profile(db, profile_id)
    node_ids: set[int] = set()
    core_ids: set[int] = set()
    for assignment in profile.assignments:
        core_ids.update(await service.cores_of_assignment(db, assignment))
        node_ids.update(await service.nodes_for_assignment(db, assignment))

    await db.delete(profile)
    await db.commit()

    for core_id in core_ids:
        try:
            await service.persist_core_rules(db, core_id, admin, allow_restart=confirm_restart, drop_unsupported=True)
        except (EnforcementError, RuleValueError) as exc:
            logger.warning(f"delete profile {profile_id}: core {core_id} not updated: {exc}")
    for node_id in node_ids:
        try:
            await service.push_live(db, node_id)
        except (EnforcementError, RuleValueError) as exc:
            logger.warning(f"delete profile {profile_id}: node {node_id} not cleaned: {exc}")


@router.get("/assignments", response_model=list[AssignmentResponse])
async def list_assignments(
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(OWNER_READ),
):
    rows = (await db.execute(select(ContentFilterAssignment).order_by(ContentFilterAssignment.id))).scalars().all()
    delivery = await _delivery_map(db, list(rows))
    return [await _assignment_view(db, row, delivery.get(row.id)) for row in rows]


@router.post("/assignments", response_model=AssignmentResponse, status_code=201)
async def create_assignment(
    payload: AssignmentPayload,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_WRITE),
):
    await _load_profile(db, payload.profile_id)

    if payload.node_id is None:
        carriers = await _carriers_of(db, payload.inbound_tag)
        if not carriers:
            raise HTTPException(status_code=404, detail="no xray node currently carries an endpoint with that name")
        for protocol in {p for _, p in carriers}:
            if protocol not in ROUTABLE_PROTOCOLS:
                raise HTTPException(
                    status_code=422,
                    detail=f"{protocol or 'this'} endpoints cannot be filtered: {UNROUTABLE_REASON}",
                )
    else:
        node = await db.get(Node, payload.node_id)
        if node is None:
            raise HTTPException(status_code=404, detail="node not found")

        cores = await _node_cores(db, node)
        if not any(core_type == "xray" for _, core_type, _ in cores):
            raise HTTPException(status_code=422, detail=NON_XRAY_REASON)

        if payload.inbound_tag:
            protocols = _xray_inbounds(cores)
            if payload.inbound_tag not in protocols:
                raise HTTPException(status_code=404, detail="that endpoint does not exist on this node's core")
            protocol = protocols[payload.inbound_tag]
            if protocol not in ROUTABLE_PROTOCOLS:
                raise HTTPException(
                    status_code=422,
                    detail=f"{protocol or 'this'} endpoints cannot be filtered: {UNROUTABLE_REASON}",
                )

    clause = (
        ContentFilterAssignment.node_id.is_(None)
        if payload.node_id is None
        else ContentFilterAssignment.node_id == payload.node_id
    )
    duplicate = (
        (
            await db.execute(
                select(ContentFilterAssignment)
                .where(clause, ContentFilterAssignment.inbound_tag == payload.inbound_tag)
                .order_by(ContentFilterAssignment.id)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="that scope already has a profile applied")

    assignment = ContentFilterAssignment(
        profile_id=payload.profile_id,
        node_id=payload.node_id,
        inbound_tag=payload.inbound_tag,
        is_enabled=payload.is_enabled,
    )
    db.add(assignment)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="that scope already has a profile applied") from None
    await db.refresh(assignment)

    applied = None
    if assignment.is_enabled:
        try:
            applied = await _safe(
                service.apply_assignment(db, assignment, admin, allow_restart=payload.confirm_restart)
            )
        except ReloadRequired as exc:
            _ask_restart(exc)
        except EnforcementError as exc:
            _fail(exc)
    return await _assignment_view(db, assignment, applied=applied)


async def _target_problem(db: AsyncSession, node_id: int, inbound_tag: str) -> str | None:
    node = await db.get(Node, node_id)
    if node is None:
        return "node not found"
    if str(getattr(node.status, "value", node.status) or "") == "disabled":
        return "this node is disabled, so the panel never connects to it"
    cores = await _node_cores(db, node)
    if not any(core_type == "xray" for _, core_type, _ in cores):
        return NON_XRAY_REASON
    if not _serves_routing(cores):
        return 'this node\'s core does not publish RoutingService; add "api": {"services": ["RoutingService"]} to it'
    if inbound_tag:
        protocols = _xray_inbounds(cores)
        if inbound_tag not in protocols:
            return "that endpoint does not exist on this node's core"
        if protocols[inbound_tag] not in ROUTABLE_PROTOCOLS:
            return f"{protocols[inbound_tag] or 'this'} endpoints cannot be filtered: {UNROUTABLE_REASON}"
    return None


def _skipped(node_id: int | None, tag: str, reason: str, detail: str) -> AssignmentOutcome:
    return AssignmentOutcome(
        node_id=node_id, inbound_tag=tag, created=False, status="skipped", reason=reason, detail=detail
    )


def _unsupported_target(node_id, tag, node_support, tag_support) -> AssignmentOutcome | None:
    if node_id is None:
        carrier = tag_support.get(tag)
        if carrier is None:
            return _skipped(None, tag, capability.NO_NODES, capability.tag_note(tag, capability.NO_NODES))
        if not carrier.supported:
            return _skipped(None, tag, carrier.reason, capability.tag_note(tag, carrier.reason))
        return None
    node = node_support.get(node_id)
    if node is not None and not node.supported:
        return _skipped(node_id, tag, node.reason, capability.node_note(node.id, node.name, node.reason))
    carrier = tag_support.get(tag) if tag else None
    if carrier is not None and node_id in carrier.unsupported_node_ids:
        return _skipped(node_id, tag, capability.PRE_ROUTED, capability.tag_note(tag, capability.PRE_ROUTED))
    return None


@router.post("/assignments/bulk", response_model=AssignmentBulkResult, status_code=201)
async def create_assignments_bulk(
    payload: AssignmentBulkPayload,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_WRITE),
):
    await _load_profile(db, payload.profile_id)

    pairs: list[tuple[int | None, str]] = []
    if payload.node_ids:
        for node_id in dict.fromkeys(payload.node_ids):
            for tag in dict.fromkeys(payload.inbound_tags or [""]):
                pairs.append((node_id, tag))
    else:
        for tag in dict.fromkeys(payload.inbound_tags):
            pairs.append((None, tag))

    report = await service.capability_report(db)
    node_support = {entry.id: entry for entry in report.nodes}
    tag_support = {entry.tag: entry for entry in report.inbound_tags}

    outcomes: list[AssignmentOutcome] = []
    pending: list[tuple[ContentFilterAssignment, AssignmentOutcome, bool]] = []
    for node_id, tag in pairs:
        unsupported = _unsupported_target(node_id, tag, node_support, tag_support)
        if unsupported is not None:
            outcomes.append(unsupported)
            continue
        if node_id is not None:
            problem = await _target_problem(db, node_id, tag)
            if problem is not None:
                outcomes.append(
                    AssignmentOutcome(node_id=node_id, inbound_tag=tag, created=False, status="skipped", detail=problem)
                )
                continue
        clause = (
            ContentFilterAssignment.node_id.is_(None) if node_id is None else ContentFilterAssignment.node_id == node_id
        )
        existing = (
            (
                await db.execute(
                    select(ContentFilterAssignment)
                    .where(clause, ContentFilterAssignment.inbound_tag == tag)
                    .order_by(ContentFilterAssignment.id)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            was_live = bool(existing.is_enabled)
            existing.profile_id = payload.profile_id
            existing.is_enabled = payload.is_enabled
            outcome = AssignmentOutcome(
                node_id=node_id, inbound_tag=tag, created=False, status="failed", detail="updated"
            )
            outcomes.append(outcome)
            pending.append((existing, outcome, was_live))
            continue
        assignment = ContentFilterAssignment(
            profile_id=payload.profile_id, node_id=node_id, inbound_tag=tag, is_enabled=payload.is_enabled
        )
        db.add(assignment)
        outcome = AssignmentOutcome(node_id=node_id, inbound_tag=tag, created=True, status="failed")
        outcomes.append(outcome)
        pending.append((assignment, outcome, False))

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="another admin changed these endpoints at the same moment; nothing was saved, try again",
        ) from None

    alive: list[tuple[ContentFilterAssignment, AssignmentOutcome, bool]] = []
    for assignment, outcome, was_live in pending:
        try:
            await db.refresh(assignment)
        except InvalidRequestError:
            outcome.enforced = False
            outcome.detail = "this assignment was removed before the filter could be applied"
            continue
        alive.append((assignment, outcome, was_live))

    try:
        delivery = await _delivery_map(db, [assignment for assignment, _, _ in alive])
    except HTTPException:
        delivery = {}
    for assignment, outcome, was_live in alive:
        outcome.delivery = delivery.get(assignment.id)
        if not assignment.is_enabled:
            outcome.status = "disabled"
            outcome.enforced = False
            if was_live:
                problem, prompt = await _withdraw_rules(db, assignment, admin, payload.confirm_restart)
                outcome.reload = prompt
                outcome.detail = problem or "switched off and removed from the nodes"
            else:
                outcome.detail = "switched off, so nothing was sent to the nodes"
            continue
        try:
            applied = (
                await _safe(service.apply_assignment(db, assignment, admin, allow_restart=payload.confirm_restart))
                or {}
            )
        except ReloadRequired as exc:
            outcome.enforced = False
            outcome.detail = exc.detail
            outcome.reload = _reload_prompt(exc)
            continue
        except service.UnsupportedNodes as exc:
            outcome.status = "skipped"
            outcome.enforced = False
            outcome.detail = exc.detail
            outcome.reason = capability.leading_reason(entry.reason for entry in exc.blockers)
            continue
        except EnforcementError as exc:
            outcome.enforced = False
            outcome.detail = exc.detail
            continue
        outcome.enforced = bool(assignment.enforced)
        outcome.status = "applied" if outcome.enforced else "failed"
        outcome.advisories = list(applied.get("advisories") or [])
        outcome.advisory_note = str(applied.get("advisory_note") or "")

    return AssignmentBulkResult(
        created=sum(1 for o in outcomes if o.created),
        updated=sum(1 for o in outcomes if not o.created and o.status != "skipped"),
        applied=sum(1 for o in outcomes if o.status == "applied"),
        failed=sum(1 for o in outcomes if o.status == "failed"),
        disabled=sum(1 for o in outcomes if o.status == "disabled"),
        skipped=sum(1 for o in outcomes if o.status == "skipped"),
        outcomes=outcomes,
    )


@router.post("/assignments/{assignment_id}/apply", response_model=AssignmentResponse)
async def apply_assignment(
    assignment_id: int,
    confirm_restart: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_WRITE),
):
    assignment = await _load_assignment(db, assignment_id)
    applied = None
    try:
        applied = await _safe(service.apply_assignment(db, assignment, admin, allow_restart=confirm_restart))
    except ReloadRequired as exc:
        _ask_restart(exc)
    except EnforcementError as exc:
        _fail(exc)
    return await _assignment_view(db, assignment, applied=applied)


@router.delete("/assignments/{assignment_id}", status_code=204)
async def delete_assignment(
    assignment_id: int,
    confirm_restart: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_WRITE),
):
    assignment = await _load_assignment(db, assignment_id)
    try:
        await service.withdraw_assignment(db, assignment, admin, allow_restart=confirm_restart)
    except ReloadRequired as exc:
        _ask_restart(exc)
    except EnforcementError as exc:
        _fail(exc)
    except RuleValueError as exc:
        logger.warning(f"assignment {assignment_id}: cleanup could not rebuild the rules: {exc}")


@router.post("/test", response_model=DestinationVerdict)
async def test_destination(
    payload: DestinationTest,
    _: AdminDetails = Depends(OWNER_READ),
):
    verdict = await service.probe(payload.node_id, payload.inbound_tag, payload.domain.strip().lower())
    return DestinationVerdict(
        domain=payload.domain.strip().lower(),
        outbound=verdict,
        blocked=verdict == "BLOCK",
    )
