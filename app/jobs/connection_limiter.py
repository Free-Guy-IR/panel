"""Record how many devices are on each subscription, on a schedule.

Writes one row per user into user_connection_states, which is what the users
list reads. Recomputing per row, or asking the nodes from a page request,
would not survive a table of four thousand users.

Acting on a user is a separate decision behind its own switch, off by default.
With it off this job only ever observes, and anyone still restricted from when
it was on is released - being left disabled by a feature that is no longer
running is the one outcome nobody would expect.
"""

import asyncio
import time
from datetime import UTC, datetime

from sqlalchemy import func, select

from app import notification, scheduler
from app.db import GetDB
from app.db.crud.settings import get_settings
from app.db.models import User, UserConnectionState
from app.jobs.dependencies import SYSTEM_ADMIN
from app.models.settings import ConnectionLimit
from app.operation import OperatorType
from app.operation.user import UserOperation
from app.utils.connection_enforcement import USER_LOAD_OPTIONS, due_to_restore, forget_old, release, restrict
from app.utils.connection_limiter import prune_out_of_scope, run_assessment
from app.utils.logger import get_logger
from config import runtime_settings

logger = get_logger("connection-limiter")

# The job is registered on a fixed one-minute tick; how often a check actually
# runs is a setting, and persistence_cycles counts those checks - so a cycle
# that comes round too early has to stand aside, or a user reaches the end of
# the ladder in half the time the settings describe.
_last_assessment = 0.0


async def _seconds_since_last_check(db) -> float | None:
    """How long ago the last cycle ran, taken from the states it wrote.

    The in-process clock starts again at every restart, and without this a
    deploy would hand every user an extra check - which on a three-cycle
    ladder is a third of the way to a disable.
    """
    last = await db.scalar(select(func.max(UserConnectionState.checked_at)))
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (datetime.now(UTC) - last).total_seconds()


user_operator = UserOperation(operator_type=OperatorType.SYSTEM)


# Re-read each cycle rather than caching, so a settings change takes effect on
# the next run instead of at the next restart.
async def _current_settings() -> ConnectionLimit | None:
    async with GetDB() as db:
        row = await get_settings(db)
        stored = getattr(row, "connection_limit", None) if row is not None else None
    if not stored:
        return None
    try:
        return ConnectionLimit.model_validate(stored)
    except Exception:
        logger.exception("connection limit settings are not valid; skipping this cycle")
        return None


async def _push_to_nodes(users) -> None:
    """Make the account change real on the nodes, and tell whoever is listening.

    The same path the panel takes when a user expires or runs out of data, so
    a user restricted here is disconnected exactly as one who ran out would be.
    """
    for user in users:
        try:
            updated = await user_operator.update_user(user)
            asyncio.create_task(notification.user_status_change(updated, SYSTEM_ADMIN))
        except Exception:
            logger.exception("could not push the change for user %s to the nodes", user.id)


def _enforcing(settings) -> bool:
    """Whether anyone may be acted on at all.

    "Monitor only" promises to record without restricting anyone, so it holds
    the whole enforcement path shut regardless of the enforcement switch.
    """
    return settings is not None and settings.enforcement_enabled and not settings.monitor_only


async def _release_due(db, settings) -> None:
    """Let go of anyone whose time is up, or of everyone when enforcement is off."""
    everyone = not _enforcing(settings)
    pairs = await due_to_restore(db, everyone=everyone)
    if not pairs:
        return

    for restriction, user in pairs:
        release(restriction, user)
    await db.commit()

    await _push_to_nodes([user for _, user in pairs])
    logger.info(
        "released %d user(s)%s",
        len(pairs),
        " because enforcement is off" if everyone else "",
    )


async def record_connection_states():
    settings = await _current_settings()

    # Ahead of every early return: a restriction that has expired must lift
    # even on a cycle where nothing else is going to run.
    async with GetDB() as db:
        await _release_due(db, settings)

    if settings is None or not settings.enabled:
        return

    global _last_assessment
    now = time.monotonic()
    # A second of tolerance: the tick and the interval drift against each other.
    due_after = settings.check_interval_seconds - 1
    if _last_assessment:
        if now - _last_assessment < due_after:
            return
    else:
        async with GetDB() as db:
            elapsed = await _seconds_since_last_check(db)
        if elapsed is not None and elapsed < due_after:
            return

    async with GetDB() as db:
        # Before anything else, so that narrowing the scope takes effect even
        # on a cycle that later runs out of time.
        forgotten = await prune_out_of_scope(db, settings)
        if forgotten:
            await db.commit()
            logger.info("forgot %d user(s) the settings no longer cover", forgotten)

        try:
            # Belt and braces: collection has its own deadline, but a cycle
            # that overruns anyway must not hold the slot against the next one.
            observations = await asyncio.wait_for(run_assessment(db, settings), timeout=120)
        except TimeoutError:
            # The slot was spent either way, so the next cycle waits its turn.
            _last_assessment = now
            logger.warning("connection check exceeded its time budget; skipping this cycle")
            return
        # Stamped once the checking has actually happened: a cycle that failed
        # outright recorded nothing and no streak moved, so it may retry.
        _last_assessment = now
        if not observations:
            return

        existing = {
            state.user_id: state
            for state in (
                await db.execute(
                    select(UserConnectionState).where(
                        UserConnectionState.user_id.in_([o.user_id for o in observations])
                    )
                )
            )
            .scalars()
            .all()
        }

        now = datetime.now(UTC)
        for obs in observations:
            state = existing.get(obs.user_id)
            if state is None:
                state = UserConnectionState(user_id=obs.user_id)
                db.add(state)
                streak = 1
            else:
                # A count that keeps coming back means more than one that
                # appeared once, so the streak is what the UI leans on.
                streak = state.streak + 1 if state.verdict == obs.verdict else 1

            state.checked_at = now
            state.devices = obs.devices
            state.address_sources = obs.address_sources
            state.hwid_count = obs.hwid_count
            state.node_count = obs.node_count
            state.app_count = obs.app_count
            state.verdict = obs.verdict
            state.limit_applied = obs.limit_applied
            state.streak = streak
            state.node_streak = obs.node_streak
            state.at_once_streak = obs.at_once_streak
            obs.streak = streak
            state.reasons = obs.reasons
            state.details = obs.details

        await db.commit()

        if _enforcing(settings):
            await _enforce(db, observations, settings)

    over = sum(1 for o in observations if o.verdict == "over_limit")
    logger.info(
        "connection check: %d users, %d over the limit of %d",
        len(observations),
        over,
        settings.device_limit,
    )


async def _enforce(db, observations, settings) -> None:
    """Act on the users whose verdict has held long enough to be believed.

    A single cycle proves nothing - an address changes, a phone switches from
    wifi to mobile - so a user is only acted on once the same verdict has come
    back persistence_cycles times running.
    """
    over = [o for o in observations if o.verdict == "over_limit" and o.streak >= settings.persistence_cycles]
    if not over:
        return

    users = {
        user.id: user
        for user in (
            # The user is loaded fresh here, so everything restrict() and
            # update_user() read off it has to come with the query (see
            # USER_LOAD_OPTIONS): an unloaded relationship raises under the
            # async session instead of loading.
            await db.execute(select(User).options(*USER_LOAD_OPTIONS).where(User.id.in_([o.user_id for o in over])))
        )
        .scalars()
        .all()
    }

    changed = []
    for observation in over:
        user = users.get(observation.user_id)
        if user is None:
            continue
        if await restrict(db, user, observation, settings) is not None:
            changed.append(user)

    await db.commit()
    if changed:
        await _push_to_nodes(changed)

    forgotten = await forget_old(db, settings.violation_retention_days)
    if forgotten:
        await db.commit()


if runtime_settings.role.runs_scheduler:
    # The interval is read from settings at each run rather than here, so the
    # job is registered at a fixed tick and exits immediately when disabled.
    scheduler.add_job(
        record_connection_states,
        "interval",
        # The tick is the finest interval the settings allow; how often a check
        # actually runs is read from the settings on each tick.
        seconds=30,
        max_instances=1,
        coalesce=True,
        id="record_connection_states",
        replace_existing=True,
    )
