"""Act on a user the checking has judged, and put them back afterwards.

Nothing here runs unless enforcement is turned on. The checking itself only
ever records, and that stays true: this module is the only place a user's
account is touched, and it is skipped entirely while the setting is off.

The escalation follows the same shape as PG-Limiter's: a warning for the first
violation, then disables of growing length, then one that only a person can
lift. Which step applies comes from how many violations the user already has
inside the window, so a user who behaves for long enough starts again from the
beginning without anyone clearing anything.

Restoring puts back exactly what was there. The status and groups are recorded
on the restriction at the moment it is applied, because reconstructing them
afterwards is guesswork once the user has been moved.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.db.models import ConnectionRestriction, User, UserStatus
from app.models.settings import ConnectionLimit
from app.utils.logger import get_logger

# Everything restrict() and update_user() read off a user has to come with
# the query. Under the async session an unloaded relationship does not load,
# it raises MissingGreenlet - which is how nobody was ever restricted.
USER_LOAD_OPTIONS = (
    joinedload(User.admin),
    joinedload(User.next_plan),
    selectinload(User.usage_logs),
    selectinload(User.groups),
)

logger = get_logger("connection-enforcement")

# A step of 0 is a warning and touches nothing; -1 disables until a person
# lifts it. Anything else is a disable of that many minutes.
WARNING_ONLY = 0
UNTIL_LIFTED = -1


def step_for(violations_in_window: int, steps: list[int]) -> tuple[int, int]:
    """Which rung this violation lands on, and the disable it carries.

    The list runs out before repeat offenders do, so the last rung repeats -
    otherwise a user past the end would fall off it and be let go.
    """
    if not steps:
        return 0, WARNING_ONLY
    index = min(violations_in_window, len(steps) - 1)
    return index, steps[index]


async def violations_in_window(db: AsyncSession, user_id: int, window_hours: int) -> int:
    """How many times this user has already been acted on, recently enough to count."""
    since = datetime.now(UTC) - timedelta(hours=window_hours)
    return (
        await db.scalar(
            select(func.count())
            .select_from(ConnectionRestriction)
            .where(ConnectionRestriction.user_id == user_id, ConnectionRestriction.created_at > since)
        )
    ) or 0


async def active_restriction(db: AsyncSession, user_id: int) -> ConnectionRestriction | None:
    return (
        await db.execute(
            select(ConnectionRestriction).where(
                ConnectionRestriction.user_id == user_id, ConnectionRestriction.active.is_(True)
            )
        )
    ).scalar()


async def restrict(
    db: AsyncSession, user: User, observation, settings: ConnectionLimit
) -> ConnectionRestriction | None:
    """Record this violation and apply whatever step it lands on.

    Returns the restriction when the user's account was actually changed, so
    the caller knows to push that change out to the nodes. A warning changes
    nothing and returns None, while still being written down - the count is
    what carries the user to the next step.
    """
    if await active_restriction(db, user.id) is not None:
        return None

    already = await violations_in_window(db, user.id, settings.violation_window_hours)
    step, minutes = step_for(already, settings.punishment_steps)

    restriction = ConnectionRestriction(
        user_id=user.id,
        ip_count=observation.devices,
        ip_limit=observation.limit_applied,
        observed_ips=(observation.details or {}).get("real_groups") or [],
        method="disable",
        step_applied=step,
        disable_minutes=minutes,
        previous_status=user.status.value if hasattr(user.status, "value") else str(user.status),
        previous_group_ids=[group.id for group in user.groups],
        active=minutes != WARNING_ONLY,
    )

    if minutes == WARNING_ONLY:
        restriction.restored_at = datetime.now(UTC)
        db.add(restriction)
        logger.info("warned %s: %d devices against a limit of %d", user.username, observation.devices, observation.limit_applied)
        return None

    if minutes != UNTIL_LIFTED:
        restriction.restore_at = datetime.now(UTC) + timedelta(minutes=minutes)

    user.status = UserStatus.disabled
    db.add(restriction)
    logger.info(
        "disabled %s for %s: %d devices against a limit of %d",
        user.username,
        "good" if minutes == UNTIL_LIFTED else f"{minutes}m",
        observation.devices,
        observation.limit_applied,
    )
    return restriction


async def due_to_restore(db: AsyncSession, *, everyone: bool = False) -> list[tuple[ConnectionRestriction, User]]:
    """Restrictions whose time is up - or all of them, when enforcement is off.

    Turning enforcement off has to let people go. Leaving them disabled by a
    feature that is no longer running is the one outcome nobody would expect.
    """
    stmt = (
        select(ConnectionRestriction, User)
        .join(User, User.id == ConnectionRestriction.user_id)
        .options(*USER_LOAD_OPTIONS)
        .where(ConnectionRestriction.active.is_(True))
    )
    if not everyone:
        stmt = stmt.where(
            ConnectionRestriction.restore_at.is_not(None),
            ConnectionRestriction.restore_at <= datetime.now(UTC),
        )
    return list((await db.execute(stmt)).all())


def release(restriction: ConnectionRestriction, user: User) -> None:
    """Put back what was there before, and close the restriction."""
    if restriction.previous_status:
        try:
            user.status = UserStatus(restriction.previous_status)
        except ValueError:
            user.status = UserStatus.active
    else:
        user.status = UserStatus.active

    restriction.active = False
    restriction.restored_at = datetime.now(UTC)


async def forget_old(db: AsyncSession, retention_days: int) -> int:
    """Drop history nobody is going to read, keeping anything still in force."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    result = await db.execute(
        delete(ConnectionRestriction).where(
            ConnectionRestriction.created_at < cutoff, ConnectionRestriction.active.is_(False)
        )
    )
    return result.rowcount or 0
