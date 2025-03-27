import asyncio

from sqlalchemy.orm import Session

from app import async_scheduler as scheduler
from app.db import GetDB
from app.db.crud import (
    get_notification_reminder,
    reset_user_by_next,
    start_user_expire,
    update_user_status,
    get_active_to_expire_users,
    get_active_to_limited_users,
    get_on_hold_to_active_users,
    get_usage_percentage_reached_users,
    get_days_left_reached_users,
)
from app.db.models import User, ReminderType, UserStatus
from app.node import node_manager as node_manager
from app.models.user import UserResponse
from app.utils.logger import get_logger
from app.utils import report
from app.utils.helpers import calculate_expiration_days, calculate_usage_percent
from app import notification
from app.jobs.dependencies import SYSTEM_ADMIN
from config import JOB_REVIEW_USERS_INTERVAL, NOTIFY_DAYS_LEFT, NOTIFY_REACHED_USAGE_PERCENT, WEBHOOK_ADDRESS


logger = get_logger("review-users")


async def add_notification_reminders(db: Session, user: User) -> None:
    if user.data_limit:
        usage_percent = calculate_usage_percent(user.used_traffic, user.data_limit)

        for percent in sorted(NOTIFY_REACHED_USAGE_PERCENT, reverse=True):
            if usage_percent >= percent:
                if not await get_notification_reminder(db, user.id, ReminderType.data_usage, threshold=percent):
                    report.data_usage_percent_reached(
                        db, usage_percent, UserResponse.model_validate(user), user.id, user.expire, threshold=percent
                    )
                break

    if user.expire:
        expire_days = calculate_expiration_days(user.expire)

        for days_left in sorted(NOTIFY_DAYS_LEFT):
            if expire_days <= days_left:
                if not await get_notification_reminder(db, user.id, ReminderType.expiration_date, threshold=days_left):
                    report.expire_days_reached(
                        db, expire_days, UserResponse.model_validate(user), user.id, user.expire, threshold=days_left
                    )
                break


async def reset_user_by_next_report(db: Session, db_user: User):
    db_user = await reset_user_by_next(db, db_user)

    user = UserResponse.model_validate(db_user)

    asyncio.create_task(node_manager.update_user(user))

    asyncio.create_task(notification.user_data_reset_by_next(user, user.admin))


async def review():
    async with GetDB() as db:

        async def change_status(db_user: User, status: UserStatus):
            db_user = await update_user_status(db, db_user, status)

            user = UserResponse.model_validate(db_user)
            asyncio.create_task(node_manager.remove_user(user))
            asyncio.create_task(notification.user_status_change(user, SYSTEM_ADMIN.username))

            logger.info(f'User "{db_user.username}" status changed to {status.value}')

            if db_user.next_plan and (db_user.next_plan.fire_on_either or (db_user.is_limited and db_user.is_expired)):
                await reset_user_by_next_report(db, db_user)

        async def activate_user(db_user: User):
            db_user = await start_user_expire(db, db_user)

            logger.info(f'User "{db_user.username}" status changed to {UserStatus.active.value}')
            user = UserResponse.model_validate(db_user)
            asyncio.create_task(notification.user_status_change(user, SYSTEM_ADMIN.username))

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
            for percentage in NOTIFY_REACHED_USAGE_PERCENT:
                users = await get_usage_percentage_reached_users(db, percentage)
                # TODO: implement async data_usage_percent_reached notfication

            for days in NOTIFY_DAYS_LEFT:
                users = await get_days_left_reached_users(db, days)
                # TODO: implement async expire_days_reached notfication


scheduler.add_job(review, "interval", seconds=JOB_REVIEW_USERS_INTERVAL, coalesce=True, max_instances=1)
