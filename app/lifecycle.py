import asyncio
import inspect
from contextlib import asynccontextmanager

from app.utils.logger import get_logger

logger = get_logger("lifecycle")

HTTP_DRAIN_TIMEOUT_S = 1.5
SHUTDOWN_BUDGET_S = 7.25
SHUTDOWN_FINAL_RESERVE_S = 0.5
SHUTDOWN_CUT_SETTLE_S = 0.2

startup_functions = []
shutdown_functions = []
final_shutdown_functions = []

_shutdown_deadline: float | None = None


def on_startup(func):
    if func not in startup_functions:
        startup_functions.append(func)
    return func


def on_shutdown(func):
    if func not in shutdown_functions:
        shutdown_functions.append(func)
    return func


def on_shutdown_first(func):
    if func in shutdown_functions:
        shutdown_functions.remove(func)
    shutdown_functions.insert(0, func)
    return func


def on_shutdown_final(func):
    if func not in final_shutdown_functions:
        final_shutdown_functions.append(func)
    return func


def shutdown_time_left() -> float | None:
    if _shutdown_deadline is None:
        return None
    return max(_shutdown_deadline - asyncio.get_running_loop().time(), 0.0)


def _accepts_app(func) -> bool:
    try:
        return "app" in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


async def _invoke(func, app):
    if not callable(func):
        return
    accepts_app = _accepts_app(func)
    if asyncio.iscoroutinefunction(func):
        if accepts_app:
            await func(app=app)
        else:
            await func()
    else:
        if accepts_app:
            func(app=app)
        else:
            func()


def _callback_name(func) -> str:
    return getattr(func, "__qualname__", None) or repr(func)


async def _run_shutdown_callbacks(functions, app, deadline: float) -> None:
    global _shutdown_deadline
    loop = asyncio.get_running_loop()
    _shutdown_deadline = deadline
    try:
        for index, func in enumerate(functions):
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.error(
                    "Shutdown budget of %ss is spent; skipped %s",
                    SHUTDOWN_BUDGET_S,
                    ", ".join(_callback_name(skipped) for skipped in functions[index:]),
                )
                return
            task = asyncio.ensure_future(_invoke(func, app))
            done, _ = await asyncio.wait({task}, timeout=max(remaining - SHUTDOWN_CUT_SETTLE_S, remaining / 2))
            if not done:
                task.cancel()
                await asyncio.wait({task}, timeout=max(deadline - loop.time(), 0.0))
                logger.error(
                    "Shutdown callback %s was cut at the %ss shutdown budget", _callback_name(func), SHUTDOWN_BUDGET_S
                )
                continue
            if not task.cancelled() and task.exception() is not None:
                logger.error(
                    "Shutdown callback %s failed; running the remaining callbacks",
                    _callback_name(func),
                    exc_info=task.exception(),
                )
    finally:
        _shutdown_deadline = None


@asynccontextmanager
async def lifespan(app):
    for func in startup_functions:
        await _invoke(func, app)
    yield

    loop = asyncio.get_running_loop()
    final_deadline = loop.time() + SHUTDOWN_BUDGET_S
    await _run_shutdown_callbacks(list(shutdown_functions), app, final_deadline - SHUTDOWN_FINAL_RESERVE_S)
    await _run_shutdown_callbacks(list(final_shutdown_functions), app, final_deadline)
