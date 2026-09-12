from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.db import AsyncSession, get_db
from app.db.models import User as DBUser
from app.fork.cores.openvpn import generate_openvpn_pki
from app.models.admin import AdminDetails
from app.models.core import MTProtoRegistrationSecret, OpenVPNPKIResponse
from app.models.user import UserStatus
from app.operation import OperatorType
from app.operation.core import CoreOperation
from app.routers.authentication import require_permission
from app.utils import responses

core_operator = CoreOperation(operator_type=OperatorType.API)
router = APIRouter(tags=["Core"], prefix="/api/core", responses={401: responses._401, 403: responses._403})

MTPROTO_REGISTRATION_SECRET_DESCRIPTION = (
    """Return a client secret that @MTProxybot accepts when registering this instance."""
)

OPENVPN_GENERATE_PKI_DESCRIPTION = """Generate a fresh OpenVPN PKI bundle (CA + server cert/key + tls-crypt static key).

Stateless - does not read or write any core config, so it is not scoped to a core_id.
The dashboard's OpenVPN core editor calls this both to populate PKI on a brand-new
(not yet created) core draft and to regenerate PKI on an existing one; the caller is
responsible for embedding the result into CoreConfig.config["pki"] before saving.
Regenerating invalidates every previously-downloaded .ovpn file for users on that core,
since the CA and tls-crypt key both change."""


@router.get(
    "/{core_id}/mtproto/{tag}/registration-secret",
    response_model=MTProtoRegistrationSecret,
    description=MTPROTO_REGISTRATION_SECRET_DESCRIPTION,
)
async def get_mtproto_registration_secret(
    core_id: int,
    tag: str,
    _: AdminDetails = Depends(require_permission("cores", "read")),
    db: AsyncSession = Depends(get_db),
) -> MTProtoRegistrationSecret:
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


@router.post(
    "/openvpn/generate-pki",
    response_model=OpenVPNPKIResponse,
    description=OPENVPN_GENERATE_PKI_DESCRIPTION,
)
async def generate_openvpn_pki_bundle(
    _: AdminDetails = Depends(require_permission("cores", "update")),
):
    return OpenVPNPKIResponse(**generate_openvpn_pki())
