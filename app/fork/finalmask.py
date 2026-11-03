from copy import deepcopy
from typing import Any

from app.models.host import FinalMask, FinalMaskTcpType, dump_final_mask_for_xray, prune_blank_values

FRAGMENT_SCALAR_KEYS = {"lengths": "length", "delays": "delay"}


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
        for key in FRAGMENT_SCALAR_KEYS:
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


def restore_fragment_scalars(final_mask: dict[str, Any]) -> dict[str, Any]:
    for settings in _fragment_settings(final_mask):
        for list_key, scalar_key in FRAGMENT_SCALAR_KEYS.items():
            entries = settings.get(list_key)
            if scalar_key not in settings and isinstance(entries, list) and entries:
                settings[scalar_key] = entries[0]
    return final_mask
