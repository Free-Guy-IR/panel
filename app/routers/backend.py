import json

import commentjson
from fastapi import APIRouter, Depends, HTTPException

from app import backend
from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.utils import responses
from .authentication import check_sudo_admin
from app.backend import XRayConfig
from app.operation import OperatorType
from app.operation.node import NodeOperator
from config import XRAY_JSON


node_operator = NodeOperator(operator_type=OperatorType.API)
router = APIRouter(tags=["Backend"], prefix="/api/backend", responses={401: responses._401})


@router.get("", responses={403: responses._403})
async def get_core_config(_: AdminDetails = Depends(check_sudo_admin)) -> dict:
    """Get the current core configuration."""
    with open(XRAY_JSON, "r") as f:
        config = commentjson.loads(f.read())

    return config


@router.put("", responses={403: responses._403})
async def modify_core_config(
    payload: dict, db: AsyncSession = Depends(get_db), admin: AdminDetails = Depends(check_sudo_admin)
) -> dict:
    """Modify the core configuration and restart the core."""
    try:
        config = XRayConfig(payload)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))

    backend.config = config
    with open(XRAY_JSON, "w") as f:
        f.write(json.dumps(payload, indent=4))

    await node_operator.restart_all_node(admin)

    await backend.hosts.update(db)

    return payload
