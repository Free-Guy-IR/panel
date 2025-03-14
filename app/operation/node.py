import asyncio

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from GozargahNodeBridge import GozargahNode, NodeAPIError

from app.operation import BaseOperator
from app.models.node import NodeCreate, NodeResponse, NodeSettings, NodesUsageResponse, NodeModify
from app.db.models import Node, NodeStatus
from app.db.crud import (
    create_node,
    get_node_by_id,
    update_node_status,
    get_nodes,
    remove_node,
    get_nodes_usage,
    update_node,
)
from app.db.base import GetDB
from app.backend import config
from app.node import get_tls, backend_users, manager as node_manager
from app.utils.logger import get_logger


logger = get_logger("node-operator")


class NodeOperator(BaseOperator):
    async def get_node_settings(self) -> NodeSettings:
        return NodeSettings(certificate=get_tls().certificate)

    async def get_node(self, db: Session, node_id: int) -> Node:
        """Dependency: Fetch node or return not found error."""
        db_node = get_node_by_id(db, node_id)
        if not db_node:
            self.raise_error(message="Node not found", code=404)
        return db_node

    async def get_db_node(self, db: Session, node_id: Node) -> NodeResponse:
        return NodeResponse.model_validate(await self.get_node(db=db, node_id=node_id))

    async def get_db_nodes(self, db: Session, offset: int | None, limit: int | None) -> list[NodeResponse]:
        return get_nodes(db=db, offset=offset, limit=limit)

    async def connect_node(self, node_id: int) -> None:
        gozargah_node: GozargahNode = await node_manager.get_node(node_id)
        if gozargah_node is None:
            return

        with GetDB() as db:
            db_node = get_node_by_id(db, node_id)

            if db_node is None:
                return

            update_node_status(db, db_node, NodeStatus.connecting)

            try:
                info = await gozargah_node.start(
                    config=config.to_json(),
                    backend_type=0,
                    users=await backend_users(inbounds=config.inbounds),
                    timeout=10,
                )
                update_node_status(
                    db=db,
                    dbnode=db_node,
                    status=NodeStatus.connected,
                    xray_version=info.core_version,
                    node_version=info.node_version,
                )
            except NodeAPIError as e:
                logger.error(f"Failed to connect node {db_node.name} with id {db_node.id}: {e.detail}")
                if e.code == -4:
                    return

                update_node_status(
                    db=db,
                    dbnode=db_node,
                    status=NodeStatus.error,
                    message=e.detail,
                )

    async def add_node(self, db: Session, new_node: NodeCreate) -> NodeResponse:
        try:
            db_node = create_node(
                db,
                Node(
                    **new_node.model_dump(
                        exclude={"id"},
                    )
                ),
            )
        except IntegrityError:
            db.rollback()
            self.raise_error(message=f'Node "{new_node.name}" already exists', code=409)

        await node_manager.update_node(db_node)

        asyncio.create_task(self.connect_node(node_id=db_node.id))

        logger.info(f'New node "{db_node.name}" with id "{db_node.id}" added')

        return NodeResponse.model_validate(db_node)

    async def modify_node(self, db: Session, node_id: Node, modified_node: NodeModify) -> NodeResponse:
        db_node: Node = await self.get_node(db=db, node_id=node_id)

        node_data = modified_node.model_dump(
            exclude={"id"},
            exclude_none=True,
        )

        for key, value in node_data.items():
            setattr(db_node, key, value)

        if db_node.status == NodeStatus.disabled:
            db_node.xray_version = None
            db_node.message = None
        else:
            db_node.status = NodeStatus.connecting

        updated_node = update_node(db, db_node)

        if updated_node.status is NodeStatus.disabled:
            await node_manager.remove_node(updated_node.id)
        else:
            await node_manager.update_node(db_node)
            asyncio.create_task(self.connect_node(node_id=db_node.id))

        logger.info(f'Node "{db_node.name}" with id "{db_node.id}" modified')

        return NodeResponse.model_validate(updated_node)

    async def remove_node(self, db: Session, node_id: Node) -> None:
        db_node: Node = await self.get_node(db=db, node_id=node_id)

        await node_manager.remove_node(db_node.id)
        remove_node(db=db, db_node=db_node)

        logger.info(f'Node "{db_node.name}" with id "{db_node.id}" deleted')

    async def restart_node(self, db: Session, node_id: Node) -> None:
        asyncio.create_task(self.connect_node(node_id))

    async def restart_all_node(self, db: Session) -> None:
        for db_node in get_nodes(db=db, enabled=True):
            await asyncio.create_task(self.connect_node(db_node))

    async def get_usage(self, db: Session, start: str = "", end: str = "") -> NodesUsageResponse:
        start, end = self.validate_dates(start, end)
        usages = get_nodes_usage(db, start, end)

        return {"usages": usages}
