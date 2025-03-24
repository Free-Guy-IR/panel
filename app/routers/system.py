from fastapi import APIRouter, Depends

from app import backend
from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.system import SystemStats
from .authentication import get_current
from app.utils import responses
from app.operation import OperatorType
from app.operation.system import SystemOperator


system_operator = SystemOperator(operator_type=OperatorType.API)
router = APIRouter(tags=["System"], prefix="/api", responses={401: responses._401})


@router.get("/system", response_model=SystemStats)
async def get_system_stats(db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(get_current)):
    """Fetch system stats including memory, CPU, and user metrics."""
    return await system_operator.get_system_stats(db, admin=admin)


@router.get("/inbounds", response_model=list[str])
async def get_inbounds(_: AdminDetails = Depends(get_current)):
    """Retrieve inbound configurations grouped by protocol."""
    return backend.config.inbounds
