import asyncio
import inspect
from contextlib import asynccontextmanager

from app.utils.logger import get_logger

logger = get_logger("lifecycle")

startup_functions = []
shutdown_functions = []


def on_startup(func):
    if func not in startup_functions:
        startup_functions.append(func)
    return func


def on_shutdown(func):
    if func not in shutdown_functions:
        shutdown_functions.append(func)
    return func


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


@asynccontextmanager
async def lifespan(app):
    for func in startup_functions:
        await _invoke(func, app)
    yield

    for func in shutdown_functions:
        try:
            await _invoke(func, app)
        except Exception:
            logger.exception("Shutdown callback %s failed; running the remaining callbacks", _callback_name(func))
