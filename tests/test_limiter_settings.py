"""Two settings the panel offers that the job was not reading.

"Monitor only" says it records without restricting anyone, and it sat next to
the enforcement switch doing nothing at all, so a panel with both on restricted
people anyway. And the check interval is what persistence_cycles counts: the
job ticks every minute regardless, so a panel set to check every two minutes
walked a user up the whole ladder in half the time its own settings described.
"""

from unittest.mock import AsyncMock, patch

import pytest

import app.jobs.connection_limiter as job
from app.models.settings import ConnectionLimit


class _Observation:
    def __init__(self, user_id=1, streak=99, verdict="over_limit"):
        self.user_id = user_id
        self.streak = streak
        self.verdict = verdict
        self.devices = 5
        self.limit_applied = 2
        self.details = {}


def _settings(**over) -> ConnectionLimit:
    base = {"enabled": True, "enforcement_enabled": True, "monitor_only": False, "check_interval_seconds": 120}
    base.update(over)
    return ConnectionLimit(**base)


def test_monitor_only_holds_the_enforcement_path_shut():
    assert job._enforcing(_settings(monitor_only=True)) is False
    assert job._enforcing(_settings(enforcement_enabled=False)) is False
    assert job._enforcing(_settings()) is True
    assert job._enforcing(None) is False


@pytest.mark.asyncio
async def test_monitor_only_releases_whoever_is_still_held():
    """Switching it on has to let go of anyone already restricted."""
    with patch.object(job, "due_to_restore", new_callable=AsyncMock, return_value=[]) as due:
        await job._release_due(AsyncMock(), _settings(monitor_only=True))

    assert due.await_args.kwargs["everyone"] is True


@pytest.mark.asyncio
async def test_enforcement_on_and_monitor_off_keeps_running_restrictions():
    with patch.object(job, "due_to_restore", new_callable=AsyncMock, return_value=[]) as due:
        await job._release_due(AsyncMock(), _settings())

    assert due.await_args.kwargs["everyone"] is False


@pytest.mark.asyncio
async def test_a_cycle_that_comes_round_too_early_stands_aside(monkeypatch):
    """persistence_cycles counts checks, so the spacing has to be the configured one."""
    monkeypatch.setattr(job, "_last_assessment", 0.0)
    clock = {"t": 1000.0}
    monkeypatch.setattr(job.time, "monotonic", lambda: clock["t"])

    settings = _settings(check_interval_seconds=120)
    with (
        patch.object(job, "_current_settings", new_callable=AsyncMock, return_value=settings),
        patch.object(job, "_release_due", new_callable=AsyncMock),
        patch.object(job, "GetDB") as get_db,
        patch.object(job, "_seconds_since_last_check", new_callable=AsyncMock, return_value=None),
        patch.object(job, "prune_out_of_scope", new_callable=AsyncMock, return_value=0) as prune,
        patch.object(job, "run_assessment", new_callable=AsyncMock, return_value=[]),
    ):
        get_db.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        get_db.return_value.__aexit__ = AsyncMock(return_value=False)

        await job.record_connection_states()
        assert prune.await_count == 1, "the first cycle runs"

        clock["t"] += 60  # the next tick, but only a minute later
        await job.record_connection_states()
        assert prune.await_count == 1, "a cycle inside the interval stands aside"

        clock["t"] += 60  # now two minutes since the last check
        await job.record_connection_states()
        assert prune.await_count == 2, "the cycle at the interval runs"


@pytest.mark.asyncio
async def test_a_restart_does_not_hand_everyone_an_extra_check(monkeypatch):
    """The in-process clock starts again; the states remember when it last ran."""
    monkeypatch.setattr(job, "_last_assessment", 0.0)
    monkeypatch.setattr(job.time, "monotonic", lambda: 42.0)

    settings = _settings(check_interval_seconds=120)
    with (
        patch.object(job, "_current_settings", new_callable=AsyncMock, return_value=settings),
        patch.object(job, "_release_due", new_callable=AsyncMock),
        patch.object(job, "GetDB") as get_db,
        patch.object(job, "_seconds_since_last_check", new_callable=AsyncMock, return_value=30.0) as since,
        patch.object(job, "prune_out_of_scope", new_callable=AsyncMock, return_value=0) as prune,
        patch.object(job, "run_assessment", new_callable=AsyncMock, return_value=[]),
    ):
        get_db.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        get_db.return_value.__aexit__ = AsyncMock(return_value=False)

        await job.record_connection_states()
        assert prune.await_count == 0, "a check 30s ago means this one waits"

        since.return_value = 200.0
        await job.record_connection_states()
        assert prune.await_count == 1, "a check long enough ago means this one runs"


@pytest.mark.asyncio
async def test_a_cycle_that_failed_outright_may_retry(monkeypatch):
    """Nothing was recorded and no streak moved, so it does not cost a slot."""
    monkeypatch.setattr(job, "_last_assessment", 0.0)
    monkeypatch.setattr(job.time, "monotonic", lambda: 900.0)

    settings = _settings(check_interval_seconds=120)
    with (
        patch.object(job, "_current_settings", new_callable=AsyncMock, return_value=settings),
        patch.object(job, "_release_due", new_callable=AsyncMock),
        patch.object(job, "GetDB") as get_db,
        patch.object(job, "_seconds_since_last_check", new_callable=AsyncMock, return_value=None),
        patch.object(job, "prune_out_of_scope", new_callable=AsyncMock, return_value=0),
        patch.object(job, "run_assessment", new_callable=AsyncMock, side_effect=RuntimeError("database went away")),
    ):
        get_db.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        get_db.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(RuntimeError):
            await job.record_connection_states()

    assert job._last_assessment == 0.0


@pytest.mark.asyncio
async def test_restrictions_are_still_lifted_on_every_tick(monkeypatch):
    """The interval holds back checking, never the release of served time."""
    monkeypatch.setattr(job, "_last_assessment", 5000.0)
    monkeypatch.setattr(job.time, "monotonic", lambda: 5001.0)

    with (
        patch.object(job, "_current_settings", new_callable=AsyncMock, return_value=_settings()),
        patch.object(job, "_release_due", new_callable=AsyncMock) as release,
        patch.object(job, "GetDB") as get_db,
    ):
        get_db.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        get_db.return_value.__aexit__ = AsyncMock(return_value=False)

        await job.record_connection_states()

    release.assert_awaited_once()


def test_the_tick_is_fine_enough_for_the_smallest_interval_allowed():
    """A panel set to check every 30s cannot be served by a 60s tick."""
    import ast
    import pathlib

    source = pathlib.Path(job.__file__).read_text()
    tick = None
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "add_job":
            for keyword in node.keywords:
                if keyword.arg == "seconds":
                    tick = ast.literal_eval(keyword.value)

    smallest = ConnectionLimit.model_fields["check_interval_seconds"].metadata
    lowest = next(m.ge for m in smallest if hasattr(m, "ge"))
    assert tick is not None and tick <= lowest, f"tick {tick}s cannot serve a {lowest}s interval"
