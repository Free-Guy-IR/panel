from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.core.openvpn import generate_openvpn_pki
from app.db import AsyncSession, get_db
from app.db.models import User as DBUser
from app.models.admin import AdminDetails
from app.models.core import (
    BulkCoreSelection,
    CoreCreate,
    CoreResponse,
    CoreResponseList,
    CoresSimpleResponse,
    MTProtoRegistrationSecret,
    OpenVPNPKIResponse,
    RemoveCoresResponse,
)
from app.models.reality_scan import RealityScanRequest, RealityScanResult
from app.models.user import UserStatus
from app.operation import OperatorType
from app.operation.core import CoreOperation
from app.operation.node import NodeOperation
from app.utils import responses

from .authentication import require_permission
from .dependencies import get_core_list_query, get_core_simple_list_query

core_operator = CoreOperation(operator_type=OperatorType.API)
node_operator = NodeOperation(operator_type=OperatorType.API)
router = APIRouter(tags=["Core"], prefix="/api/core", responses={401: responses._401, 403: responses._403})


@router.post("", response_model=CoreResponse, status_code=status.HTTP_201_CREATED)
async def create_core_config(
    new_core: CoreCreate,
    admin: AdminDetails = Depends(require_permission("cores", "create")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new core configuration."""
    return await core_operator.create_core(db, new_core, admin)


@router.post("/reality-scan", response_model=RealityScanResult)
async def scan_reality_target(
    request: RealityScanRequest,
    _: AdminDetails = Depends(require_permission("cores", "read")),
):
    return await core_operator.scan_reality_target(request)


@router.get("/{core_id}", response_model=CoreResponse)
async def get_core_config(
    core_id: int, _: AdminDetails = Depends(require_permission("cores", "read")), db: AsyncSession = Depends(get_db)
) -> dict:
    """Get a core configuration by its ID."""
    return await core_operator.get_validated_core_config(db, core_id)


@router.get("/{core_id}/mtproto/{tag}/registration-secret", response_model=MTProtoRegistrationSecret)
async def get_mtproto_registration_secret(
    core_id: int,
    tag: str,
    _: AdminDetails = Depends(require_permission("cores", "read")),
    db: AsyncSession = Depends(get_db),
) -> MTProtoRegistrationSecret:
    """Return a client secret that @MTProxybot accepts when registering this instance."""
    core = await core_operator.get_validated_core_config(db, core_id)
    config = core["config"] if isinstance(core, dict) else core.config
    instance = next((i for i in config.get("instances", []) if i.get("tag") == tag), None)
    if instance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"no mtproto instance tagged {tag}")

    mode = instance.get("mode") or "faketls"
    domains = instance.get("fake_tls_domains") or (
        [instance["fake_tls_domain"]] if instance.get("fake_tls_domain") else []
    )
    if mode == "faketls" and not domains:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{tag} has no fake-tls domain")

    stmt = (
        select(DBUser.proxy_settings)
        .where(DBUser.status == UserStatus.active)
        .where(DBUser.proxy_settings["mtproto"]["secret"].isnot(None))
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    raw_secret = (row or {}).get("mtproto", {}).get("secret") if row else None
    if not raw_secret:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no active user has an mtproto secret yet - create one first",
        )

    if mode == "plain":
        secret = "dd" + raw_secret
        domain = None
    else:
        domain = domains[0]
        secret = "ee" + raw_secret + domain.encode("ascii").hex()

    return MTProtoRegistrationSecret(tag=tag, port=instance.get("port"), mode=mode, domain=domain, secret=secret)


@router.put("/{core_id}", response_model=CoreResponse)
async def modify_core_config(
    core_id: int,
    restart_nodes: bool,
    modified_core: CoreCreate,
    admin: AdminDetails = Depends(require_permission("cores", "update")),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing core configuration."""
    response = await core_operator.modify_core(db, core_id, modified_core, admin)

    if restart_nodes:
        await node_operator.restart_all_node(db=db, core_id=core_id, admin=admin)

    return response


@router.delete("/{core_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_core_config(
    core_id: int,
    restart_nodes: bool = False,
    admin: AdminDetails = Depends(require_permission("cores", "delete")),
    db: AsyncSession = Depends(get_db),
):
    """Delete a core configuration."""
    await core_operator.delete_core(db, core_id, admin)

    if restart_nodes:
        await node_operator.restart_all_node(db=db, core_id=core_id, admin=admin)

    return {}


@router.get("s", response_model=CoreResponseList)
async def get_all_cores(
    query=Depends(get_core_list_query),
    _: AdminDetails = Depends(require_permission("cores", "read")),
    db: AsyncSession = Depends(get_db),
):
    """Get a list of all core configurations."""
    return await core_operator.get_all_cores(db, query)


@router.get(
    "s/simple",
    response_model=CoresSimpleResponse,
    summary="Get lightweight core list",
    description="Returns only id and name for cores. Optimized for dropdowns and autocomplete.",
)
async def get_cores_simple(
    query=Depends(get_core_simple_list_query),
    _: AdminDetails = Depends(require_permission("cores", "read_simple")),
    db: AsyncSession = Depends(get_db),
):
    """Get lightweight core list with only id and name"""
    return await core_operator.get_cores_simple(db=db, query=query)


@router.post("/{core_id}/restart", status_code=status.HTTP_204_NO_CONTENT)
async def restart_core(
    core_id: int,
    admin: AdminDetails = Depends(require_permission("cores", "update")),
    db: AsyncSession = Depends(get_db),
):
    """restart nodes related to the core config"""

    await node_operator.restart_all_node(db=db, core_id=core_id, admin=admin)
    return {}


@router.post("/openvpn/generate-pki", response_model=OpenVPNPKIResponse)
async def generate_openvpn_pki_bundle(
    _: AdminDetails = Depends(require_permission("cores", "update")),
):
    """Generate a fresh OpenVPN PKI bundle (CA + server cert/key + tls-crypt static key).

    Stateless - does not read or write any core config, so it is not scoped to a core_id.
    The dashboard's OpenVPN core editor calls this both to populate PKI on a brand-new
    (not yet created) core draft and to regenerate PKI on an existing one; the caller is
    responsible for embedding the result into CoreConfig.config["pki"] before saving.
    Regenerating invalidates every previously-downloaded .ovpn file for users on that core,
    since the CA and tls-crypt key both change.
    """
    return OpenVPNPKIResponse(**generate_openvpn_pki())


@router.post(
    "s/bulk/delete",
    response_model=RemoveCoresResponse,
    responses={400: responses._400, 403: responses._403, 404: responses._404},
)
async def bulk_delete_cores(
    bulk_cores: BulkCoreSelection,
    db: AsyncSession = Depends(get_db),
    admin: AdminDetails = Depends(require_permission("cores", "delete")),
):
    """Delete selected cores by ID."""
    return await core_operator.bulk_remove_cores(db, bulk_cores, admin)
