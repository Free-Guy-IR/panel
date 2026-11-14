from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Node, node_additional_cores_association


async def get_node_ids_by_core(db: AsyncSession, core_id: int) -> list[int]:
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


async def get_node_versions_by_core(db: AsyncSession, core_id: int) -> list[tuple[str, str | None]]:
    if core_id == 1:
        primary_match = or_(Node.core_config_id == core_id, Node.core_config_id.is_(None))
    else:
        primary_match = Node.core_config_id == core_id

    extra_match = Node.id.in_(
        select(node_additional_cores_association.c.node_id).where(
            node_additional_cores_association.c.core_config_id == core_id
        )
    )
    stmt = select(Node.name, Node.node_version).where(or_(primary_match, extra_match))
    return [(row[0], row[1]) for row in (await db.execute(stmt)).all()]
