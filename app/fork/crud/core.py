from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node, node_additional_cores_association


async def get_node_ids_by_core(db: AsyncSession, core_id: int) -> list[int]:
    """
    IDs of nodes using a core as their primary or an additional core.

    Must be collected BEFORE the core is deleted: the deletion sweeps the
    association rows (and DB FKs null the primary link), so afterwards the
    query would find nothing.

    Args:
        db (AsyncSession): The database session.
        core_id (int): The ID of the core configuration.

    Returns:
        list[int]: IDs of affected nodes.
    """
    if core_id == 1:
        primary_match = or_(Node.core_config_id == core_id, Node.core_config_id.is_(None))
    else:
        primary_match = Node.core_config_id == core_id

    extra_match = Node.id.in_(
        select(node_additional_cores_association.c.node_id).where(
            node_additional_cores_association.c.core_config_id == core_id
        )
    )
    stmt = select(Node.id).where(or_(primary_match, extra_match))
    return list((await db.execute(stmt)).scalars().all())
