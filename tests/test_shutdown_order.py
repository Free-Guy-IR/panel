import pytest

from app import lifecycle


def _name(func) -> str:
    return f"{getattr(func, '__module__', '')}.{getattr(func, '__qualname__', repr(func))}"


@pytest.fixture
def shutdown_order():
    from app.app_factory import create_app

    create_app()
    return [_name(func) for func in lifecycle.shutdown_functions]


def test_the_scheduler_stops_before_anything_its_jobs_depend_on_is_torn_down(shutdown_order):
    stop = next(i for i, name in enumerate(shutdown_order) if name.endswith("_stop_scheduler_and_leader"))
    torn_down_first = [name for name in shutdown_order[:stop]]
    assert torn_down_first == [], (
        "these run while APScheduler can still start new ticks (record_user_usages reads node stats with "
        "reset=True), and before drain_usage_jobs gets its 8 s grace inside Docker's 10 s stop window:\n  "
        + "\n  ".join(torn_down_first)
    )
