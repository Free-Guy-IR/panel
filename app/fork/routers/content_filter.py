import json
from datetime import UTC, datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.crud.core import get_core_config_by_id
from app.db.models import Node
from app.fork.content_filter import service
from app.fork.content_filter.catalog import catalog_payload
from app.fork.content_filter.schemas import (
    AssignmentPayload,
    AssignmentResponse,
    CatalogResponse,
    DestinationTest,
    DestinationVerdict,
    ProfilePayload,
    ProfileResponse,
    TargetInbound,
    TargetNode,
)
from app.fork.content_filter.service import EnforcementError
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

ROUTABLE_PROTOCOLS = frozenset({"vless", "vmess", "trojan", "shadowsocks", "socks", "http"})
UNROUTABLE_REASON = "this protocol has no domain routing engine"
NON_XRAY_REASON = "only xray cores can enforce destination rules"


def _fail(exc: EnforcementError):
    raise HTTPException(status_code=exc.code, detail=exc.detail)


async def _load_profile(db: AsyncSession, profile_id: int) -> ContentFilterProfile:
    profile = await db.get(ContentFilterProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="filter profile not found")
    return profile


async def _carriers_of(db: AsyncSession, inbound_tag: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in (await db.execute(select(Node))).scalars().all():
        core = await get_core_config_by_id(db, node.core_config_id)
        if core is None or str(getattr(core.type, "value", core.type) or "") != "xray":
            continue
        config = core.config if isinstance(core.config, dict) else json.loads(core.config or "{}")
        for inbound in config.get("inbounds") or []:
            if str(inbound.get("tag") or "") == inbound_tag:
                found.append((node.id, str(inbound.get("protocol") or "").lower()))
    return found


async def _load_assignment(db: AsyncSession, assignment_id: int) -> ContentFilterAssignment:
    assignment = await db.get(ContentFilterAssignment, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail="assignment not found")
    return assignment


@router.get("/catalog", response_model=CatalogResponse)
async def get_catalog(_: AdminDetails = Depends(OWNER_READ)):
    return catalog_payload()


@router.get("/targets", response_model=list[TargetNode])
async def get_targets(
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(OWNER_READ),
):
    nodes = (await db.execute(select(Node))).scalars().all()
    out: list[TargetNode] = []
    for node in nodes:
        core = await get_core_config_by_id(db, node.core_config_id)
        config = {}
        core_type = ""
        if core is not None:
            core_type = str(getattr(core.type, "value", core.type) or "")
            config = core.config if isinstance(core.config, dict) else json.loads(core.config or "{}")
        is_xray = core_type == "xray"
        services = ((config.get("api") or {}).get("services")) or []
        inbounds: list[TargetInbound] = []
        for inbound in config.get("inbounds") or []:
            protocol = str(inbound.get("protocol") or "").lower()
            tag = str(inbound.get("tag") or "")
            if not tag or tag == "API_INBOUND":
                continue
            if not is_xray:
                inbounds.append(TargetInbound(tag=tag, protocol=protocol, filterable=False, reason=NON_XRAY_REASON))
            elif protocol not in ROUTABLE_PROTOCOLS:
                inbounds.append(TargetInbound(tag=tag, protocol=protocol, filterable=False, reason=UNROUTABLE_REASON))
            else:
                inbounds.append(TargetInbound(tag=tag, protocol=protocol, filterable=True))
        out.append(
            TargetNode(
                id=node.id,
                name=node.name,
                status=str(getattr(node.status, "value", node.status) or ""),
                core_config_id=node.core_config_id,
                routing_service=is_xray and any(str(s).lower() == "routingservice" for s in services),
                inbounds=inbounds,
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
            await service.apply_assignment(db, assignment, admin)
        except EnforcementError as exc:
            logger.warning(f"profile {profile_id}: re-apply on node {assignment.node_id} failed: {exc.detail}")
    return ProfileResponse.model_validate(profile)


@router.delete("/profiles/{profile_id}", status_code=204)
async def delete_profile(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_WRITE),
):
    profile = await _load_profile(db, profile_id)
    node_ids = {assignment.node_id for assignment in profile.assignments}
    core_ids: set[int] = set()
    for node_id in node_ids:
        node = await db.get(Node, node_id)
        if node is not None:
            core_ids.add(node.core_config_id)

    await db.delete(profile)
    await db.commit()

    for core_id in core_ids:
        try:
            await service.persist_core_rules(db, core_id, admin)
        except EnforcementError as exc:
            logger.warning(f"delete profile {profile_id}: core {core_id} not updated: {exc.detail}")
    for node_id in node_ids:
        try:
            await service.push_live(db, node_id)
        except EnforcementError as exc:
            logger.warning(f"delete profile {profile_id}: node {node_id} not cleaned: {exc.detail}")


@router.get("/assignments", response_model=list[AssignmentResponse])
async def list_assignments(
    db: AsyncSession = Depends(get_db),
    _: AdminDetails = Depends(OWNER_READ),
):
    rows = (await db.execute(select(ContentFilterAssignment).order_by(ContentFilterAssignment.id))).scalars().all()
    return [AssignmentResponse.model_validate(row) for row in rows]


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

        core = await get_core_config_by_id(db, node.core_config_id)
        if core is None or str(getattr(core.type, "value", core.type) or "") != "xray":
            raise HTTPException(status_code=422, detail=NON_XRAY_REASON)

        config = core.config if isinstance(core.config, dict) else json.loads(core.config or "{}")
        if payload.inbound_tag:
            match = next(
                (i for i in config.get("inbounds") or [] if str(i.get("tag") or "") == payload.inbound_tag), None
            )
            if match is None:
                raise HTTPException(status_code=404, detail="that endpoint does not exist on this node's core")
            protocol = str(match.get("protocol") or "").lower()
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
        await db.execute(
            select(ContentFilterAssignment).where(clause, ContentFilterAssignment.inbound_tag == payload.inbound_tag)
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="that scope already has a profile applied")

    assignment = ContentFilterAssignment(
        profile_id=payload.profile_id,
        node_id=payload.node_id,
        inbound_tag=payload.inbound_tag,
        is_enabled=payload.is_enabled,
    )
    db.add(assignment)
    await db.commit()
    await db.refresh(assignment)

    if assignment.is_enabled:
        try:
            await service.apply_assignment(db, assignment, admin)
        except EnforcementError as exc:
            _fail(exc)
    return AssignmentResponse.model_validate(assignment)


@router.post("/assignments/{assignment_id}/apply", response_model=AssignmentResponse)
async def apply_assignment(
    assignment_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_WRITE),
):
    assignment = await _load_assignment(db, assignment_id)
    try:
        await service.apply_assignment(db, assignment, admin)
    except EnforcementError as exc:
        _fail(exc)
    await db.refresh(assignment)
    return AssignmentResponse.model_validate(assignment)


@router.delete("/assignments/{assignment_id}", status_code=204)
async def delete_assignment(
    assignment_id: int,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(OWNER_WRITE),
):
    assignment = await _load_assignment(db, assignment_id)
    try:
        await service.withdraw_assignment(db, assignment, admin)
    except EnforcementError as exc:
        _fail(exc)


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
