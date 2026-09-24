import unicodedata
from collections.abc import Iterable
from ipaddress import ip_address

CONFIG_BREAKING_CHARACTERS = frozenset('<>"')
LINE_BREAKING_CATEGORIES = frozenset({"Zl", "Zp"})
HOST_ADDRESS_PUNCTUATION = frozenset(".-_:[]*{}")
DNS_NAME_PUNCTUATION = frozenset(".-_")
WIREGUARD_MAX_KEEPALIVE = 65535


def _is_control(character: str) -> bool:
    category = unicodedata.category(character)
    return category.startswith("C") or category in LINE_BREAKING_CATEGORIES


def is_safe_config_value(value) -> bool:
    text = str(value)
    return not any(character in CONFIG_BREAKING_CHARACTERS or _is_control(character) for character in text)


def all_safe_config_values(values: Iterable) -> bool:
    return all(is_safe_config_value(value) for value in values)


def is_safe_host_address(value: str) -> bool:
    return bool(value) and all(
        character.isalnum() or character in HOST_ADDRESS_PUNCTUATION for character in value
    )


def is_safe_dns_name(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character in DNS_NAME_PUNCTUATION for character in value)


def check_host_addresses(addresses):
    if addresses is None:
        return None
    for address in addresses:
        if address and not is_safe_host_address(address):
            raise ValueError(f"address {address!r} may only contain letters, digits and . - _ : [ ] * {{ }}")
    return addresses


def check_dns_servers(servers, *, allow_names: bool):
    if not servers:
        return servers
    checked = []
    for server in servers:
        if not isinstance(server, str):
            raise ValueError("dns servers must be strings")  # noqa: TRY004
        server = server.strip()
        try:
            ip_address(server)
        except ValueError:
            if not (allow_names and is_safe_dns_name(server)):
                raise ValueError(f"dns server {server!r} is not a valid IP address") from None
        checked.append(server)
    return checked


def check_single_line(value, field: str):
    if value is not None and not is_safe_config_value(value):
        raise ValueError(f"{field} must not contain control characters, <, > or \"")
    return value


def wireguard_keepalive(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return min(value, WIREGUARD_MAX_KEEPALIVE)
