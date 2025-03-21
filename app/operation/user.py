import asyncio
from sqlalchemy.exc import IntegrityError
from app.backend import config
from app.db import Session
from app.db.crud import (
    create_user,
    get_admin,
    remove_user,
    reset_user_data_usage,
    revoke_user_sub,
    update_user,
)
from app.db.models import Group, NextPlan, User
from app.models.admin import Admin
from app.models.user import UserCreate, UserModify, UserResponse, UserStatus
from app.node import manager as node_manager
from app.operation import BaseOperator
from app.utils.logger import get_logger

logger = get_logger("user-operator")


class UserOperator(BaseOperator):
    async def add_user(self, db: Session, new_user: UserCreate, admin: Admin) -> UserResponse:
        if new_user.next_plan is not None and new_user.next_plan.user_template_id is not None:
            await self.get_validated_user_template(db, new_user.next_plan.user_template_id)

        user_data = new_user.model_dump(
            exclude={"next_plan", "expire", "proxy_settings", "group_ids"}, exclude_none=True
        )
        all_groups = await self.validate_all_groups(db, new_user)

        db_admin = get_admin(db, admin.username)
        db_user = User(
            **user_data,
            proxy_settings=new_user.proxy_settings.dict(),
            expire=(new_user.expire or None),
            admin=db_admin,
            groups=all_groups,
            next_plan=NextPlan(**new_user.next_plan.model_dump()) if new_user.next_plan else None,
        )
        try:
            db_user = create_user(db, db_user)
        except IntegrityError:
            db.rollback()
            self.raise_error(message="User already exists", code=409)

        user = UserResponse.model_validate(db_user)

        asyncio.create_task(node_manager.update_user(user, inbounds=config.inbounds))

        logger.info(f'New user "{db_user.username}" with id "{db_user.id}" added by admin "{admin.username}"')

        return user

    async def validate_all_groups(self, db, user: UserResponse) -> list[Group]:
        all_groups = []
        if user.group_ids:
            for group_id in user.group_ids:
                db_group = await self.get_validated_group(db, group_id)
                all_groups.append(db_group)
        return all_groups

    async def modify_user(self, db: Session, username: str, modified_user: UserModify, admin: Admin) -> UserResponse:
        db_user: User = await self.get_validated_user(db, username, admin)
        if modified_user.group_ids:
            await self.validate_all_groups(db, modified_user)

        if modified_user.next_plan is not None and modified_user.next_plan.user_template_id is not None:
            await self.get_validated_user_template(db, modified_user.next_plan.user_template_id)

        old_status = db_user.status
        logger.info(modified_user.proxy_settings)

        db_user = update_user(db, db_user, modified_user)
        user = UserResponse.model_validate(db_user)

        if db_user.status in (UserStatus.active, UserStatus.on_hold):
            asyncio.create_task(node_manager.update_user(user, inbounds=config.inbounds))
        else:
            asyncio.create_task(node_manager.remove_user(user))

        logger.info(f'User "{user.username}" with id "{db_user.id}" modified by admin "{admin.username}"')

        if user.status != old_status:
            logger.info(f'User "{db_user.username}" status changed from "{old_status.value}" to "{user.status.value}"')

        return user

    async def remove_user(self, db: Session, username: str, admin: Admin):
        db_user: User = await self.get_validated_user(db, username, admin)

        remove_user(db, db_user)
        user = UserResponse.model_validate(db_user)
        asyncio.create_task(node_manager.remove_user(user))

        logger.info(f'User "{db_user.username}" with id "{db_user.id}" deleted by admin "{admin.username}"')
        return {}

    async def reset_user_data_usage(self, db: Session, username: str, admin: Admin):
        db_user: User = await self.get_validated_user(db, username, admin)

        db_user = reset_user_data_usage(db=db, db_user=db_user)
        user = UserResponse.model_validate(db_user)
        if db_user.status in (UserStatus.active, UserStatus.on_hold):
            asyncio.create_task(node_manager.update_user(user, inbounds=config.inbounds))

        logger.info(f'User "{db_user.username}" usage was reset by admin "{admin.username}"')

        return {}

    async def revoke_user_sub(self, db: Session, username: str, admin: Admin):
        db_user: User = await self.get_validated_user(db, username, admin)

        db_user = revoke_user_sub(db=db, db_user=db_user)
        user = UserResponse.model_validate(db_user)
        if db_user.status in (UserStatus.active, UserStatus.on_hold):
            asyncio.create_task(node_manager.update_user(user, inbounds=config.inbounds))

        logger.info(f'User "{db_user.username}" subscription was revoked by admin "{admin.username}"')

        return {}
