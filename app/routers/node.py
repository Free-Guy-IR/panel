import asyncio
import time

from fastapi import APIRouter, Depends, WebSocket
from starlette.websockets import WebSocketDisconnect

from app import backend
from app.db import Session, get_db
from app.models.admin import Admin
from app.models.node import (
    NodeCreate,
    NodeModify,
    NodeResponse,
    NodeSettings,
    NodesUsageResponse,
)
from app.operation.node import NodeOperator
from app.operation import OperatorType
from app.utils import responses


node_operator = NodeOperator(operator_type=OperatorType.API)
router = APIRouter(tags=["Node"], prefix="/api/node", responses={401: responses._401, 403: responses._403})


@router.get("/settings", response_model=NodeSettings)
async def get_node_settings(_: Admin = Depends(Admin.check_sudo_admin)):
    """Retrieve the current node settings, including TLS certificate."""
    return await node_operator.get_node_settings()


@router.post("", response_model=NodeResponse, responses={409: responses._409})
async def add_node(
    new_node: NodeCreate,
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Add a new node to the database and optionally add it as a host."""
    return await node_operator.add_node(db, new_node)


@router.get("/{node_id}", response_model=NodeResponse)
async def get_node(
    node_id: int,
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Retrieve details of a specific node by its ID."""
    return await node_operator.get_db_node(db=db, node_id=node_id)


@router.websocket("/{node_id}/logs")
async def node_logs(node_id: int, websocket: WebSocket, db: Session = Depends(get_db)):
    token = websocket.query_params.get("token") or websocket.headers.get("Authorization", "").removeprefix("Bearer ")
    admin = Admin.get_admin(token, db)
    if not admin:
        return await websocket.close(reason="Unauthorized", code=4401)

    if not admin.is_sudo:
        return await websocket.close(reason="You're not allowed", code=4403)

    if not backend.nodes.get(node_id):
        return await websocket.close(reason="Node not found", code=4404)

    if not backend.nodes[node_id].connected:
        return await websocket.close(reason="Node is not connected", code=4400)

    interval = websocket.query_params.get("interval")
    if interval:
        try:
            interval = float(interval)
        except ValueError:
            return await websocket.close(reason="Invalid interval value", code=4400)
        if interval > 10:
            return await websocket.close(reason="Interval must be more than 0 and at most 10 seconds", code=4400)

    await websocket.accept()

    cache = ""
    last_sent_ts = 0
    node = backend.nodes[node_id]
    with node.get_logs() as logs:
        while True:
            if not node == backend.nodes[node_id]:
                break

            if interval and time.time() - last_sent_ts >= interval and cache:
                try:
                    await websocket.send_text(cache)
                except (WebSocketDisconnect, RuntimeError):
                    break
                cache = ""
                last_sent_ts = time.time()

            if not logs:
                try:
                    await asyncio.wait_for(websocket.receive(), timeout=0.2)
                    continue
                except asyncio.TimeoutError:
                    continue
                except (WebSocketDisconnect, RuntimeError):
                    break

            log = logs.popleft()

            if interval:
                cache += f"{log}\n"
                continue

            try:
                await websocket.send_text(log)
            except (WebSocketDisconnect, RuntimeError):
                break


@router.get("s", response_model=list[NodeResponse])
async def get_nodes(
    offset: int = None, limit: int = None, db: Session = Depends(get_db), _: Admin = Depends(Admin.check_sudo_admin)
):
    """Retrieve a list of all nodes. Accessible only to sudo admins."""
    return await node_operator.get_db_nodes(db=db, offset=offset, limit=limit)


@router.put("/{node_id}", response_model=NodeResponse)
async def modify_node(
    modified_node: NodeModify,
    node_id: int,
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Update a node's details. Only accessible to sudo admins."""
    return await node_operator.modify_node(db, node_id=node_id, modified_node=modified_node)


@router.post("/{node_id}/reconnect")
async def reconnect_node(
    node_id: int,
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Trigger a reconnection for the specified node. Only accessible to sudo admins."""
    await node_operator.restart_node(db=db, node_id=node_id)
    return {}


@router.delete("/{node_id}")
async def remove_node(
    node_id: int,
    db: Session = Depends(get_db),
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Delete a node and remove it from xray in the background."""
    await node_operator.remove_node(db=db, node_id=node_id)
    return {}


@router.get("s/usage", response_model=NodesUsageResponse)
async def get_usage(
    db: Session = Depends(get_db),
    start: str = "",
    end: str = "",
    _: Admin = Depends(Admin.check_sudo_admin),
):
    """Retrieve usage statistics for nodes within a specified date range."""
    return await node_operator.get_usage(db=db, start=start, end=end)
