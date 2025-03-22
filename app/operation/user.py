import asyncio
from datetime import datetime as dt

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
    get_users,
    reset_all_users_data_usage,
    get_user_usages,
    reset_user_by_next,
    set_owner,
    get_all_users_usages,
    get_expired_users,
    delete_expired_users,
    UsersSortingOptions,
)
from app.db.models import User
from app.models.admin import Admin
from app.models.user import (
    UserCreate,
    UserModify,
    UserResponse,
    UserStatus,
    UsersResponse,
    UserUsagesResponse,
    UsersUsagesResponse,
    RemoveUsersResponse,
)
from app.node import manager as node_manager
from app.operation import BaseOperator
from app.utils.logger import get_logger

logger = get_logger("user-operator")


class UserOperator(BaseOperator):
    async def add_user(self, db: Session, new_user: UserCreate, admin: Admin) -> UserResponse:
        if new_user.next_plan is not None and new_user.next_plan.user_template_id is not None:
            await self.get_validated_user_template(db, new_user.next_plan.user_template_id)

        all_groups = await self.validate_all_groups(db, new_user)
        db_admin = get_admin(db, admin.username)

        try:
            db_user = create_user(db, new_user, all_groups, db_admin)
        except IntegrityError:
            db.rollback()
            self.raise_error(message="User already exists", code=409)

        user = UserResponse.model_validate(db_user)

        asyncio.create_task(node_manager.update_user(user, inbounds=config.inbounds))

        logger.info(f'New user "{db_user.username}" with id "{db_user.id}" added by admin "{admin.username}"')

        return user

    async def modify_user(self, db: Session, username: str, modified_user: UserModify, admin: Admin) -> UserResponse:
        db_user: User = await self.get_validated_user(db, username, admin)
        if modified_user.group_ids:
            await self.validate_all_groups(db, modified_user)

        if modified_user.next_plan is not None and modified_user.next_plan.user_template_id is not None:
            await self.get_validated_user_template(db, modified_user.next_plan.user_template_id)

        old_status = db_user.status

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

    async def reset_users_data_usage(self, db: Session, admin: Admin):
        """Reset all users data usage"""
        db_admin = await self.get_validated_admin(db, admin.username)
        reset_all_users_data_usage(db=db, admin=db_admin)

    async def active_next_plan(self, db: Session, username: str, admin: Admin) -> UserResponse:
        """Reset user by next plan"""
        db_user: User = await self.get_validated_user(db, username, admin)

        if db_user is None or db_user.next_plan is None:
            self.raise_error(message="User doesn't have next plan", code=404)

        db_user = reset_user_by_next(db=db, dbuser=db_user)

        user = UserResponse.model_validate(db_user)
        if db_user.status in (UserStatus.active, UserStatus.on_hold):
            asyncio.create_task(node_manager.update_user(user, inbounds=config.inbounds))

        logger.info(f'User "{db_user.username}"\'s usage was reset by next plan by admin "{admin.username}"')

        return user

    async def set_owner(self, db: Session, username: str, admin_username: str, admin: Admin) -> UserResponse:
        """Set a new owner (admin) for a user."""
        new_admin = await self.get_validated_admin(db, username=admin_username)
        db_user: User = await self.get_validated_user(db, username, admin)

        db_user = set_owner(db, db_user, new_admin)
        user = UserResponse.model_validate(db_user)

        logger.info(f'{user.username}"owner successfully set to{new_admin.username} by admin "{admin.username}"')

        return user

    async def get_user_usage(
        self, db: Session, username: str, admin: Admin, start: str = "", end: str = ""
    ) -> UserUsagesResponse:
        start, end = self.validate_dates(start, end)
        db_user: User = await self.get_validated_user(db, username, admin)

        usages = get_user_usages(db, db_user, start, end)

        return UserUsagesResponse(username=username, usages=usages)

    async def get_users(
        self,
        db: Session,
        admin: Admin,
        offset: int = None,
        limit: int = None,
        username: list[str] = None,
        search: str | None = None,
        owner: list[str] | None = None,
        status: UserStatus | None = None,
        sort: str | None = None,
    ) -> UsersResponse:
        """Get all users"""
        sort_list = []
        if sort is not None:
            opts = sort.strip(",").split(",")
            for opt in opts:
                try:
                    sort_list.append(UsersSortingOptions[opt])
                except KeyError:
                    self.raise_error(message=f'"{opt}" is not a valid sort option', code=400)

        users, count = get_users(
            db=db,
            offset=offset,
            limit=limit,
            search=search,
            usernames=username,
            status=status,
            sort=sort_list,
            admins=owner if admin.is_sudo else [admin.username],
            return_with_count=True,
        )

        return UsersResponse(users=users, total=count)

    async def get_users_usage(
        self,
        db: Session,
        admin: Admin,
        start: str = "",
        end: str = "",
        owner: list[str] | None = None,
    ) -> UsersUsagesResponse:
        """Get all users usage"""
        start, end = self.validate_dates(start, end)

        usages = get_all_users_usages(db=db, start=start, end=end, admin=owner if admin.is_sudo else [admin.username])

        return UsersUsagesResponse(usages=usages)

    @staticmethod
    async def remove_users_logger(users: list[str], by: str):
        for user in users:
            logger.info(f'User "{user}" deleted by admin "{by}"')

    async def get_expired_users(
        self, db: Session, admin: Admin, expired_after: dt | None = None, expired_before: dt | None = None
    ) -> list[str]:
        """
        Get users who have expired within the specified date range.

        - **expired_after** UTC datetime (optional)
        - **expired_before** UTC datetime (optional)
        - At least one of expired_after or expired_before must be provided for filtering
        - If both are omitted, returns all expired users
        """

        expired_after, expired_before = self.validate_dates(expired_after, expired_before)
        if not admin.is_sudo:
            id = await self.get_validated_admin(db, admin.username).id
        else:
            id = None

        return get_expired_users(db, expired_after, expired_before, id)

    async def delete_expired_users(
        self, db: Session, admin: Admin, expired_after: dt | None = None, expired_before: dt | None = None
    ) -> RemoveUsersResponse:
        """
        Delete users who have expired within the specified date range.

        - **expired_after** UTC datetime (optional)
        - **expired_before** UTC datetime (optional)
        - At least one of expired_after or expired_before must be provided
        """
        expired_after, expired_before = self.validate_dates(expired_after, expired_before)
        if not admin.is_sudo:
            id = await self.get_validated_admin(db, admin.username).id
        else:
            id = None

        removed_users, count = delete_expired_users(db, expired_after, expired_before, id)

        asyncio.create_task(self.remove_users_logger(users=removed_users, by=admin.username))

        return RemoveUsersResponse(users=removed_users, count=count)
