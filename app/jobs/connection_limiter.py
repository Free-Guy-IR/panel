"""Record how many devices are on each subscription, on a schedule.

Writes one row per user into user_connection_states, which is what the users
list reads. Recomputing per row, or asking the nodes from a page request,
would not survive a table of four thousand users.

The job only observes. Acting on a user is a separate decision that has not
been enabled.
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select

from app import scheduler
from app.db import GetDB
from app.db.crud.settings import get_settings
from app.db.models import UserConnectionState
from app.models.settings import ConnectionLimit
from app.utils.connection_limiter import run_assessment
from app.utils.logger import get_logger
from config import runtime_settings

logger = get_logger("connection-limiter")

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


async def record_connection_states():
    settings = await _current_settings()
    if settings is None or not settings.enabled:
        return

    async with GetDB() as db:
        try:
            # Belt and braces: collection has its own deadline, but a cycle
            # that overruns anyway must not hold the slot against the next one.
            observations = await asyncio.wait_for(run_assessment(db, settings), timeout=120)
        except TimeoutError:
            logger.warning("connection check exceeded its time budget; skipping this cycle")
            return
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
            state.streak = streak
            state.reasons = obs.reasons
            state.details = obs.details

        await db.commit()

    over = sum(1 for o in observations if o.verdict == "over_limit")
    logger.info(
        "connection check: %d users, %d over the limit of %d",
        len(observations),
        over,
        settings.device_limit,
    )


if runtime_settings.role.runs_scheduler:
    # The interval is read from settings at each run rather than here, so the
    # job is registered at a fixed tick and exits immediately when disabled.
    scheduler.add_job(
        record_connection_states,
        "interval",
        seconds=60,
        max_instances=1,
        coalesce=True,
        id="record_connection_states",
        replace_existing=True,
    )
