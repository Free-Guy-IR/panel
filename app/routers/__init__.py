import threading

from . import (
    admin,
    admin_role,
    api_key,
    client_template,
    core,
    group,
    home,
    host,
    hwid,
    node,
    settings,
    setup,
    subscription,
    system,
    user,
    user_template,
)

routers = [
    home.router,
    admin.router,
    api_key.router,
    admin_role.router,
    setup.router,
    system.router,
    settings.router,
    group.router,
    core.router,
    client_template.router,
    host.router,
    node.router,
    user.router,
    subscription.router,
    user_template.router,
    hwid.router,
]

_api_router = None
_api_router_lock = threading.RLock()
_api_router_building = False


def _build_api_router():
    from fastapi import APIRouter

    from app.fork.routers import include_fork_routers

    api_router = APIRouter()
    include_fork_routers(api_router)
    for router in routers:
        api_router.include_router(router)
    return api_router


def _get_api_router():
    global _api_router, _api_router_building
    if _api_router is not None:
        return _api_router
    with _api_router_lock:
        if _api_router is not None:
            return _api_router
        if _api_router_building:
            raise RuntimeError("app.routers.api_router was accessed while it is still being built")
        _api_router_building = True
        try:
            built = _build_api_router()
        finally:
            _api_router_building = False
        _api_router = built
    return _api_router


def __getattr__(name: str):
    if name == "api_router":
        return _get_api_router()
    raise AttributeError(name)


__all__ = ["api_router"]
