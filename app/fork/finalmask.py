from copy import deepcopy
from typing import Any

from app.models.host import (
    FinalMask,
    FinalMaskTcpType,
    FinalMaskUdpType,
    dump_final_mask_for_xray,
    prune_blank_values,
)

FRAGMENT_LIST_KEYS = ("lengths", "delays")
DOH_RESOLVER_SCHEME = "https://"


def _fragment_settings(final_mask: dict[str, Any]) -> list[dict[str, Any]]:
    tcp_layers = final_mask.get("tcp")
    if not isinstance(tcp_layers, list):
        return []
    return [
        layer["settings"]
        for layer in tcp_layers
        if isinstance(layer, dict)
        and layer.get("type") == FinalMaskTcpType.fragment.value
        and isinstance(layer.get("settings"), dict)
    ]


def prune_fragment_list_entries(final_mask: FinalMask | dict[str, Any]) -> dict[str, Any]:
    if isinstance(final_mask, FinalMask):
        result = final_mask.model_dump(exclude_none=True, by_alias=True, mode="json")
    else:
        result = deepcopy(final_mask)

    for settings in _fragment_settings(result):
        for key in FRAGMENT_LIST_KEYS:
            entries = settings.get(key)
            if not isinstance(entries, list):
                continue
            kept = [entry for entry in entries if prune_blank_values(entry) is not None]
            if kept:
                settings[key] = kept
            else:
                settings.pop(key)

    return result


def dump_fork_final_mask_for_xray(final_mask: FinalMask | dict[str, Any]) -> dict[str, Any]:
    return dump_final_mask_for_xray(prune_fragment_list_entries(final_mask))


def _xdns_resolvers(final_mask: dict[str, Any]) -> list[dict[str, Any]]:
    udp_layers = final_mask.get("udp")
    if not isinstance(udp_layers, list):
        return []
    resolvers = []
    for layer in udp_layers:
        if not isinstance(layer, dict) or layer.get("type") != FinalMaskUdpType.xdns.value:
            continue
        settings = layer.get("settings")
        if isinstance(settings, dict) and isinstance(settings.get("resolvers"), list):
            resolvers.extend(resolver for resolver in settings["resolvers"] if isinstance(resolver, dict))
    return resolvers


def _is_doh_resolver(resolver: dict[str, Any]) -> bool:
    addrs = resolver.get("addrs")
    if not isinstance(addrs, list):
        return False
    return any(isinstance(addr, str) and addr.startswith(DOH_RESOLVER_SCHEME) for addr in addrs)


def restore_doh_tls_configs(final_mask: Any) -> Any:
    if not isinstance(final_mask, dict):
        return final_mask
    for resolver in _xdns_resolvers(final_mask):
        if _is_doh_resolver(resolver) and not isinstance(resolver.get("tlsConfig"), dict):
            resolver["tlsConfig"] = {}
    return final_mask


def restore_nested_doh_tls_configs(data: Any) -> Any:
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "finalmask":
                restore_doh_tls_configs(value)
            restore_nested_doh_tls_configs(value)
    elif isinstance(data, list):
        for item in data:
            restore_nested_doh_tls_configs(item)
    return data
