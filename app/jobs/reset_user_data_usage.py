import asyncio
from datetime import datetime as dt, timedelta as td, timezone as tz

from app import logger, async_scheduler as scheduler
from app.db import GetDB, crud
from app.db.models import UserStatus, UserDataLimitResetStrategy, User
from app.models.user import UserResponse
from app import notification
from app.backend import config
from app.node import node_manager
from app.jobs.dependencies import SYSTEM_ADMIN

reset_strategy_to_days = {
    UserDataLimitResetStrategy.day.value: 1,
    UserDataLimitResetStrategy.week.value: 7,
    UserDataLimitResetStrategy.month.value: 30,
    UserDataLimitResetStrategy.year.value: 365,
}


async def reset_user_data_usage():
    now = dt.now(tz.utc)
    async with GetDB() as db:

        async def check_user(db_user: User):
            last_reset_time = db_user.last_traffic_reset_time
            num_days_to_reset = reset_strategy_to_days[db_user.data_limit_reset_strategy]

            if not (now - last_reset_time.replace(tzinfo=tz.utc)).days >= num_days_to_reset:
                return

            old_status = db_user.status

            db_user = await crud.reset_user_data_usage(db, db_user)
            user = UserResponse.model_validate(db_user)
            asyncio.create_task(notification.reset_user_data_usage(user, SYSTEM_ADMIN.username))

            if old_status != db_user.status:
                asyncio.create_task(notification.user_status_change(user, SYSTEM_ADMIN.username))

            # make user active if limited on usage reset
            if user.status == UserStatus.active:
                asyncio.create_task(node_manager.update_user(user=user, inbounds=config.inbounds))

            logger.info(f'User data usage reset for User "{user.username}"')

        users = await crud.get_users(
            db,
            status=[UserStatus.active, UserStatus.limited],
            reset_strategy=[
                UserDataLimitResetStrategy.day.value,
                UserDataLimitResetStrategy.week.value,
                UserDataLimitResetStrategy.month.value,
                UserDataLimitResetStrategy.year.value,
            ],
        )
        if users:
            await asyncio.gather(*[check_user(user) for user in users])


scheduler.add_job(
    reset_user_data_usage,
    "interval",
    minutes=10,
    coalesce=True,
    start_date=dt.now(tz.utc) + td(minutes=1),
    max_instances=1,
)
