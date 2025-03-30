import asyncio

from sqlalchemy.orm import Session

from app import async_scheduler as scheduler
from app.db import GetDB
from app.db.crud import (
    reset_user_by_next,
    start_user_expire,
    update_user_status,
    get_active_to_expire_users,
    get_active_to_limited_users,
    get_on_hold_to_active_users,
    get_usage_percentage_reached_users,
    get_days_left_reached_users,
)
from app.db.models import User, UserStatus
from app.node import node_manager as node_manager
from app.models.user import UserResponse
from app.utils.logger import get_logger
from app import notification
from app.jobs.dependencies import SYSTEM_ADMIN
from config import JOB_REVIEW_USERS_INTERVAL, NOTIFY_DAYS_LEFT, NOTIFY_REACHED_USAGE_PERCENT, WEBHOOK_ADDRESS


logger = get_logger("review-users")


async def reset_user_by_next_report(db: Session, db_user: User):
    db_user = await reset_user_by_next(db, db_user)

    user = UserResponse.model_validate(db_user)

    asyncio.create_task(node_manager.update_user(user))

    asyncio.create_task(notification.user_data_reset_by_next(user, SYSTEM_ADMIN))


async def review():
    async with GetDB() as db:

        async def change_status(db_user: User, status: UserStatus):
            db_user = await update_user_status(db, db_user, status)

            user = UserResponse.model_validate(db_user)
            asyncio.create_task(node_manager.remove_user(user))
            asyncio.create_task(notification.user_status_change(user, SYSTEM_ADMIN))

            logger.info(f'User "{db_user.username}" status changed to {status.value}')

            if db_user.next_plan and (db_user.next_plan.fire_on_either or (db_user.is_limited and db_user.is_expired)):
                await reset_user_by_next_report(db, db_user)

        async def activate_user(db_user: User):
            db_user = await start_user_expire(db, db_user)

            logger.info(f'User "{db_user.username}" status changed to {UserStatus.active.value}')
            user = UserResponse.model_validate(db_user)
            asyncio.create_task(notification.user_status_change(user, SYSTEM_ADMIN))

        users = await get_active_to_expire_users(db)
        if users:
            await asyncio.gather(*[change_status(user, UserStatus.expired) for user in users])

        users = await get_active_to_limited_users(db)
        if users:
            await asyncio.gather(*[change_status(user, UserStatus.limited) for user in users])

        users = await get_on_hold_to_active_users(db)
        if users:
            await asyncio.gather(*[activate_user(user) for user in users])

        if WEBHOOK_ADDRESS:
            for percent in NOTIFY_REACHED_USAGE_PERCENT:
                users = await get_usage_percentage_reached_users(db, percent)
                if users:
                    await asyncio.gather(
                        *[
                            notification.data_usage_percent_reached(
                                db, user.usage_percentage, UserResponse.model_validate(user), percent
                            )
                            for user in users
                        ]
                    )

            for days in NOTIFY_DAYS_LEFT:
                users = await get_days_left_reached_users(db, days)
                if users:
                    await asyncio.gather(
                        *[
                            notification.expire_days_reached(
                                db, user.days_left, UserResponse.model_validate(user), days
                            )
                            for user in users
                        ]
                    )


scheduler.add_job(review, "interval", seconds=JOB_REVIEW_USERS_INTERVAL, coalesce=True, max_instances=1)
