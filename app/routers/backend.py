from fastapi import APIRouter, Depends, status

from app.backend.hosts import hosts as hosts_storage
from app.db import AsyncSession, get_db
from app.models.admin import AdminDetails
from app.models.backend import BackendCreate, BackendResponse, BackendResponseList
from app.utils import responses
from .authentication import check_sudo_admin
from app.operation import OperatorType
from app.operation.backend import BackendOperation
from app.operation.node import NodeOperator


backend_operator = BackendOperation(operator_type=OperatorType.API)
node_operator = NodeOperator(operator_type=OperatorType.API)
router = APIRouter(tags=["Backend"], prefix="/api/backend", responses={401: responses._401, 403: responses._403})


@router.post("", response_model=BackendResponse, status_code=status.HTTP_201_CREATED)
async def create_backend_config(
    new_backend: BackendCreate, admin: AdminDetails = Depends(check_sudo_admin), db: AsyncSession = Depends(get_db)
):
    """Create a new backend configuration."""
    response = await backend_operator.add_backend(db, new_backend, admin)
    await hosts_storage.update(db)
    return response


@router.get("/{backend_id}", response_model=BackendResponse)
async def get_backend_config(
    backend_id: int, _: AdminDetails = Depends(check_sudo_admin), db: AsyncSession = Depends(get_db)
) -> dict:
    """Get a backend configuration by its ID."""
    return await backend_operator.get_validated_backend_config(db, backend_id)


@router.put("/{backend_id}", response_model=BackendResponse)
async def modify_backend_config(
    backend_id: int,
    restart_nodes: bool,
    modified_backend: BackendCreate,
    admin: AdminDetails = Depends(check_sudo_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update an existing backend configuration."""
    response = await backend_operator.modify_backend(db, backend_id, modified_backend, admin)
    await hosts_storage.update(db)

    if restart_nodes:
        await node_operator.restart_all_node(db=db, backend_id=backend_id, admin=admin)

    return response


@router.delete("/{backend_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_backend_config(
    backend_id: int,
    restart_nodes: bool = False,
    admin: AdminDetails = Depends(check_sudo_admin),
    db: AsyncSession = Depends(get_db),
):
    """Delete a backend configuration."""
    await backend_operator.delete_backend(db, backend_id, admin)
    await hosts_storage.update(db)

    if restart_nodes:
        await node_operator.restart_all_node(db=db, backend_id=backend_id, admin=admin)

    return {}


@router.get("s", response_model=BackendResponseList)
async def get_all_backends(
    offset: int | None = None,
    limit: int | None = None,
    _: AdminDetails = Depends(check_sudo_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get a list of all backend configurations."""
    return await backend_operator.get_all_backends(db, offset, limit)
