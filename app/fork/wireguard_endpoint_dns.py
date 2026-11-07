from ipaddress import ip_address
from typing import Any

ENDPOINT_DNS_TAG = "wireguard-endpoint-dns"
ENDPOINT_DIRECT_TAG = "wireguard-endpoint-direct"
UNROUTED_DNS_SCHEME_SUFFIX = "+local://"


def _endpoint_host(endpoint: Any) -> str | None:
    if not isinstance(endpoint, str) or ":" not in endpoint:
        return None
    host = endpoint.rsplit(":", 1)[0].strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host or None


def _is_domain(host: str) -> bool:
    try:
        ip_address(host)
    except ValueError:
        return True
    return False


def wireguard_endpoint_domains(outbounds: Any) -> list[str]:
    domains: list[str] = []
    if not isinstance(outbounds, list):
        return domains
    for outbound in outbounds:
        if not isinstance(outbound, dict) or outbound.get("protocol") != "wireguard":
            continue
        settings = outbound.get("settings")
        peers = settings.get("peers") if isinstance(settings, dict) else None
        for peer in peers if isinstance(peers, list) else []:
            host = _endpoint_host(peer.get("endpoint")) if isinstance(peer, dict) else None
            if host and _is_domain(host) and host not in domains:
                domains.append(host)
    return domains


def _is_routed(address: str) -> bool:
    lowered = address.strip().lower()
    return lowered != "localhost" and UNROUTED_DNS_SCHEME_SUFFIX not in lowered


def _endpoint_servers(servers: list, domains: list[str]) -> list[dict]:
    clones: list[dict] = []
    for server in servers:
        if isinstance(server, str):
            target = {"address": server}
        elif isinstance(server, dict) and not server.get("skipFallback"):
            target = {key: server[key] for key in ("address", "port", "queryStrategy") if key in server}
        else:
            continue
        address = target.get("address")
        if not isinstance(address, str) or not address.strip() or address.strip().lower() == "fakedns":
            continue
        clone = {**target, "domains": [f"full:{domain}" for domain in domains], "skipFallback": True}
        if _is_routed(address):
            clone["tag"] = ENDPOINT_DNS_TAG
        if clone not in clones:
            clones.append(clone)
    return clones if any("tag" in clone for clone in clones) else []


def resolve_wireguard_endpoints_directly(config: dict, host_outbounds: list) -> dict:
    domains = wireguard_endpoint_domains(host_outbounds)
    dns = config.get("dns")
    if not domains or not isinstance(dns, dict) or not isinstance(dns.get("servers"), list):
        return config
    clones = _endpoint_servers(dns["servers"], domains)
    if not clones:
        return config

    config["dns"] = {**dns, "servers": clones + dns["servers"]}

    routing = config.get("routing") if isinstance(config.get("routing"), dict) else {}
    rules = routing.get("rules") if isinstance(routing.get("rules"), list) else []
    rule = {"type": "field", "inboundTag": [ENDPOINT_DNS_TAG], "outboundTag": ENDPOINT_DIRECT_TAG}
    config["routing"] = {**routing, "rules": [rule, *rules]}

    outbounds = config["outbounds"]
    if not any(isinstance(o, dict) and o.get("tag") == ENDPOINT_DIRECT_TAG for o in outbounds):
        config["outbounds"] = [*outbounds, {"protocol": "freedom", "tag": ENDPOINT_DIRECT_TAG}]
    return config
