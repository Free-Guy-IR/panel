from app import backend
from app.db import AsyncSession
from app.db.models import Admin
from app.db.crud import create_group, get_group, update_group, remove_group
from app.models.group import Group, GroupCreate, GroupModify, GroupsResponse
from app.operation import BaseOperator
from app.utils.logger import get_logger

logger = get_logger("group-operator")


class GroupOperation(BaseOperator):
    async def check_inbound_tags(self, tags: list[str]) -> None:
        for tag in tags:
            if tag not in backend.config.inbounds_by_tag:
                self.raise_error(f"{tag} not found", 400)

    async def add_group(self, db: AsyncSession, new_group: GroupCreate, admin: Admin) -> Group:
        await self.check_inbound_tags(new_group.inbound_tags)
        group = await create_group(db, new_group)
        logger.info(f'Group "{group.name}" created by admin "{admin.username}"')
        return group

    async def get_all_groups(self, db: AsyncSession, offset: int, limit: int) -> GroupsResponse:
        dbgroups, count = await get_group(db, offset, limit)
        return {"groups": dbgroups, "total": count}

    async def modify_group(self, db: AsyncSession, group_id: int, modified_group: GroupModify, admin: Admin) -> Group:
        dbgroup = await self.get_validated_group(db, group_id)
        if modified_group.inbound_tags:
            await self.check_inbound_tags(modified_group.inbound_tags)
        group = await update_group(db, dbgroup, modified_group)
        # TODO: add users to nodes
        logger.info(f'Group "{group.name}" modified by admin "{admin.username}"')
        return group

    async def delete_group(self, db: AsyncSession, group_id: int, admin: Admin) -> None:
        dbgroup = await self.get_validated_group(db, group_id)
        await remove_group(db, dbgroup)
        logger.info(f'Group "{dbgroup.name}" deleted by admin "{admin.username}"')
        # TODO: remove users from nodes
