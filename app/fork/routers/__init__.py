from importlib import import_module

from fastapi import APIRouter
from fastapi.routing import APIRoute

from app.fork.registry import extra_routers, register_router, registered_router_keys

FORK_ROUTER_MODULES = (
    ("connection_limit", "connection_limit"),
    ("content_filter", "content_filter"),
    ("core", "fork_core"),
    ("node", "fork_node"),
    ("subscription", "fork_subscription"),
    ("traffic_log", "traffic_log"),
    ("user", "fork_user"),
)


def _route_keys(routers) -> dict[tuple[str, str], str]:
    keys: dict[tuple[str, str], str] = {}
    for router in routers:
        for route in getattr(router, "routes", []):
            if not isinstance(route, APIRoute):
                continue
            name = getattr(route, "name", "?")
            for method in sorted(route.methods or ()):
                keys[(route.path, method)] = name
    return keys


def _assert_no_route_collisions(fork_routers, upstream_routers) -> None:
    fork_keys = _route_keys(fork_routers)
    upstream_keys = _route_keys(upstream_routers)
    clashes = sorted(set(fork_keys) & set(upstream_keys))
    if clashes:
        detail = ", ".join(f"{m} {p} (fork={fork_keys[(p, m)]} upstream={upstream_keys[(p, m)]})" for p, m in clashes)
        raise RuntimeError(f"fork routes shadow upstream routes: {detail}")


def include_fork_routers(api_router: APIRouter) -> None:
    from app.routers import routers as upstream_routers

    for mod_name, key in FORK_ROUTER_MODULES:
        qual = f"app.fork.routers.{mod_name}"
        module = import_module(qual)
        router = getattr(module, "router", None)
        if router is None:
            raise RuntimeError(f"fork router module {qual} defines no 'router' attribute")
        register_router(router, name=key)

    expected = {key for _, key in FORK_ROUTER_MODULES}
    missing = sorted(expected - registered_router_keys())
    if missing:
        raise RuntimeError(f"fork routers failed to register: {missing}")

    fork_routers = extra_routers()
    _assert_no_route_collisions(fork_routers, upstream_routers)

    for router in fork_routers:
        api_router.include_router(router)
