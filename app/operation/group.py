import asyncio

from app.db import AsyncSession
from app.db.models import Admin
from app.db.crud import create_group, get_group, update_group, remove_group, get_users
from app.models.group import Group, GroupCreate, GroupModify, GroupsResponse, GroupResponse
from app.models.user import UserResponse
from app.node import node_manager
from app.core.manager import core_manager
from app.operation import BaseOperator
from app.utils.logger import get_logger
from app import notification

logger = get_logger("group-operator")


class GroupOperation(BaseOperator):
    async def add_group(self, db: AsyncSession, new_group: GroupCreate, admin: Admin) -> Group:
        await self.check_inbound_tags(new_group.inbound_tags)

        db_group = await create_group(db, new_group)

        group = GroupResponse.model_validate(db_group)

        asyncio.create_task(notification.create_group(group, admin.username))

        logger.info(f'Group "{group.name}" created by admin "{admin.username}"')
        return group

    async def get_all_groups(self, db: AsyncSession, offset: int| None = None, limit: int|None=None) -> GroupsResponse:
        db_groups, count = await get_group(db, offset, limit)
        return GroupsResponse.model_validate({"groups": db_groups, "total": count})

    async def modify_group(self, db: AsyncSession, group_id: int, modified_group: GroupModify, admin: Admin) -> Group:
        db_group = await self.get_validated_group(db, group_id)
        if modified_group.inbound_tags:
            await self.check_inbound_tags(modified_group.inbound_tags)
        db_group = await update_group(db, db_group, modified_group)

        users = await get_users(db, group_ids=[db_group.id])
        await asyncio.gather(
            *[
                node_manager.update_user(
                    UserResponse.model_validate(user), user.inbounds(await core_manager.get_inbounds())
                )
                for user in users
            ]
        )

        group = GroupResponse.model_validate(db_group)

        asyncio.create_task(notification.modify_group(group, admin.username))

        logger.info(f'Group "{group.name}" modified by admin "{admin.username}"')
        return group

    async def delete_group(self, db: AsyncSession, group_id: int, admin: Admin) -> None:
        db_group = await self.get_validated_group(db, group_id)

        users = await get_users(db, group_ids=[db_group.id])
        username_list = [user.username for user in users]

        await remove_group(db, db_group)
        users = await get_users(db, usernames=username_list)

        await asyncio.gather(
            *[
                node_manager.update_user(
                    UserResponse.model_validate(user), user.inbounds(await core_manager.get_inbounds())
                )
                for user in users
            ]
        )

        logger.info(f'Group "{db_group.name}" deleted by admin "{admin.username}"')

        asyncio.create_task(notification.remove_group(db_group.id, admin.username))
