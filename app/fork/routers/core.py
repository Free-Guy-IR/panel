from fastapi import APIRouter, Depends

from app.fork.cores.openvpn import generate_openvpn_pki
from app.models.admin import AdminDetails
from app.models.core import OpenVPNPKIResponse
from app.routers.authentication import require_permission
from app.utils import responses

router = APIRouter(tags=["Core"], prefix="/api/core", responses={401: responses._401, 403: responses._403})

OPENVPN_GENERATE_PKI_DESCRIPTION = """Generate a fresh OpenVPN PKI bundle (CA + server cert/key + tls-crypt static key).

Stateless - does not read or write any core config, so it is not scoped to a core_id.
The dashboard's OpenVPN core editor calls this both to populate PKI on a brand-new
(not yet created) core draft and to regenerate PKI on an existing one; the caller is
responsible for embedding the result into CoreConfig.config["pki"] before saving.
Regenerating invalidates every previously-downloaded .ovpn file for users on that core,
since the CA and tls-crypt key both change."""


@router.post(
    "/openvpn/generate-pki",
    response_model=OpenVPNPKIResponse,
    description=OPENVPN_GENERATE_PKI_DESCRIPTION,
)
async def generate_openvpn_pki_bundle(
    _: AdminDetails = Depends(require_permission("cores", "update")),
):
    return OpenVPNPKIResponse(**generate_openvpn_pki())
