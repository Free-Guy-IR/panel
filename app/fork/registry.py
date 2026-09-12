from collections.abc import Callable
from typing import Any

_CORE_TYPES: dict[Any, type] = {}
_ROUTERS: list[Any] = []
_ROUTER_KEYS: dict[Any, Any] = {}
_SUB_FORMATS: dict[str, Callable[..., Any]] = {}


def register_core_type(core_type: Any, core_cls: type) -> None:
    existing = _CORE_TYPES.get(core_type)
    if existing is not None and existing is not core_cls:
        raise ValueError(f"core type {core_type!r} already registered as {existing!r}")
    _CORE_TYPES[core_type] = core_cls


def extra_core_types() -> dict[Any, type]:
    return dict(_CORE_TYPES)


extra_core_classes = extra_core_types


def register_router(router: Any, *, name: str | None = None) -> None:
    key = name or getattr(router, "prefix", None) or id(router)
    existing = _ROUTER_KEYS.get(key)
    if existing is not None:
        if existing is router:
            return
        raise ValueError(f"router key {key!r} already registered with a different router")
    _ROUTER_KEYS[key] = router
    _ROUTERS.append(router)


def extra_routers() -> list[Any]:
    return list(_ROUTERS)


def registered_router_keys() -> set[Any]:
    return set(_ROUTER_KEYS)


def register_subscription_format(key: str, factory: Callable[..., Any]) -> None:
    _SUB_FORMATS[key] = factory


def extra_subscription_formats() -> dict[str, Callable[..., Any]]:
    return dict(_SUB_FORMATS)


def get_subscription_format(key: str) -> Callable[..., Any] | None:
    return _SUB_FORMATS.get(key)
