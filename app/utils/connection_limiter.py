import asyncio
import ipaddress
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Node,
    NodeUserUsage,
    User,
    UserConnectionLimit,
    UserConnectionState,
    UserHWID,
    UserSubscriptionUpdate,
)
from app.models.settings import ConnectionLimit
from app.node import node_manager
from app.utils.logger import get_logger

logger = get_logger("connection-limiter")

STARTED_AT = datetime.now(UTC)

RECORDER_ONLINE_AT_THROTTLE = timedelta(minutes=1)

DEFAULT_CDN_RANGES = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
    "23.235.32.0/20", "43.249.72.0/22", "103.244.50.0/24", "103.245.222.0/23",
    "103.245.224.0/24", "104.156.80.0/20", "140.248.64.0/18", "140.248.128.0/17",
    "146.75.0.0/16", "151.101.0.0/16", "157.52.64.0/18", "167.82.0.0/17",
    "167.82.128.0/20", "167.82.160.0/20", "167.82.224.0/20", "172.111.64.0/18",
    "185.31.16.0/22", "199.27.72.0/21", "199.232.0.0/16",
    "2a04:4e40::/32", "2a04:4e42::/32",
]

WITHIN_LIMIT = "within_limit"
AT_LIMIT = "at_limit"
OVER_LIMIT = "over_limit"


def _networks(ranges: list[str]) -> list[ipaddress._BaseNetwork]:
    nets = []
    for text in ranges:
        try:
            nets.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            logger.warning("ignoring invalid CDN range %r", text)
    return nets


def parse_ip(text: str):
    try:
        return ipaddress.ip_address(text.strip().strip("[]"))
    except ValueError:
        return None


def group_key(ip, settings: ConnectionLimit, pools: frozenset[str] = frozenset()) -> str:
    if pools:
        pool = pool_key(ip, settings)
        if pool in pools:
            return pool
    prefix = settings.ipv4_group_prefix if ip.version == 4 else settings.ipv6_group_prefix
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


def pool_key(ip, settings: ConnectionLimit) -> str:
    prefix = settings.carrier_pool_prefix if ip.version == 4 else settings.carrier_pool_prefix_v6
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


@dataclass
class Observation:
    user_id: int
    username: str
    devices: int = 0
    address_sources: int = 0
    hwid_count: int = 0
    hwid_devices: int = 0
    node_count: int = 0
    node_streak: int = 0
    at_once: int = 0
    at_once_streak: int = 0
    app_count: int = 0
    verdict: str = WITHIN_LIMIT
    streak: int = 0
    limit_applied: int = 0
    reasons: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def scope_conditions(settings: ConnectionLimit) -> list:
    from app.db.models import users_groups_association

    conditions = [User.status == "active"]

    if settings.apply_to_admin_ids:
        conditions.append(User.admin_id.in_(settings.apply_to_admin_ids))

    if settings.apply_to_group_ids:
        conditions.append(
            User.id.in_(
                select(users_groups_association.c.user_id).where(
                    users_groups_association.c.groups_id.in_(settings.apply_to_group_ids)
                )
            )
        )

    return conditions


async def candidate_users(db: AsyncSession, settings: ConnectionLimit) -> list[User]:
    since = datetime.now(UTC) - timedelta(seconds=settings.online_window_seconds) - RECORDER_ONLINE_AT_THROTTLE
    stmt = select(User).where(User.online_at.is_not(None), User.online_at > since, *scope_conditions(settings))
    return list((await db.execute(stmt)).scalars().all())


async def prune_out_of_scope(db: AsyncSession, settings: ConnectionLimit) -> int:
    covered = select(User.id).where(*scope_conditions(settings))
    exempt = select(UserConnectionLimit.user_id).where(UserConnectionLimit.exempt.is_(True))
    stale = max(
        datetime.now(UTC) - timedelta(hours=settings.state_max_age_hours),
        STARTED_AT,
    )

    result = await db.execute(
        delete(UserConnectionState).where(
            or_(
                UserConnectionState.user_id.not_in(covered),
                UserConnectionState.user_id.in_(exempt),
                UserConnectionState.checked_at < stale,
            )
        )
    )
    return result.rowcount or 0


@dataclass
class NodeActivity:
    touched: set[int] = field(default_factory=set)
    used: set[int] = field(default_factory=set)
    concurrent: int = 0
    concurrent_nodes: dict[int, int] = field(default_factory=dict)
    traffic: dict[int, int] = field(default_factory=dict)


def nodes_actually_used(traffic_by_node: dict[int, int], settings: ConnectionLimit) -> set[int]:
    floor = settings.node_min_traffic_kb * 1024
    return {node_id for node_id, traffic in traffic_by_node.items() if traffic >= floor}


async def nodes_per_user(
    db: AsyncSession, settings: ConnectionLimit, user_ids: list[int]
) -> dict[int, NodeActivity]:
    if not user_ids:
        return {}

    since = datetime.now(UTC) - timedelta(minutes=settings.node_window_minutes)
    rows = (
        await db.execute(
            select(
                NodeUserUsage.user_id,
                NodeUserUsage.created_at,
                NodeUserUsage.node_id,
                func.sum(NodeUserUsage.used_traffic),
            )
            .where(
                NodeUserUsage.user_id.in_(user_ids),
                NodeUserUsage.created_at > since,
                NodeUserUsage.node_id.is_not(None),
                NodeUserUsage.used_traffic > 0,
            )
            .group_by(NodeUserUsage.user_id, NodeUserUsage.created_at, NodeUserUsage.node_id)
        )
    ).all()

    buckets: dict[int, dict[object, dict[int, int]]] = defaultdict(lambda: defaultdict(dict))
    totals: dict[int, dict[int, int]] = defaultdict(dict)
    for user_id, bucket, node_id, traffic in rows:
        traffic = int(traffic or 0)
        buckets[user_id][bucket][node_id] = traffic
        totals[user_id][node_id] = totals[user_id].get(node_id, 0) + traffic

    out: dict[int, NodeActivity] = {}
    for user_id, per_bucket in buckets.items():
        activity = NodeActivity(
            touched=set(totals[user_id]),
            used=nodes_actually_used(totals[user_id], settings),
            traffic=dict(totals[user_id]),
        )
        for nodes in per_bucket.values():
            real = {node_id: nodes[node_id] for node_id in nodes_actually_used(nodes, settings)}
            if len(real) > activity.concurrent:
                activity.concurrent = len(real)
                activity.concurrent_nodes = real
        out[user_id] = activity

    return out


async def per_user_limits(db: AsyncSession, user_ids: list[int]) -> dict[int, int]:
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(UserConnectionLimit.user_id, UserConnectionLimit.ip_limit).where(
                UserConnectionLimit.user_id.in_(user_ids),
                UserConnectionLimit.ip_limit.is_not(None),
                UserConnectionLimit.exempt.is_(False),
            )
        )
    ).all()
    return {user_id: limit for user_id, limit in rows}


async def prior_node_streaks(db: AsyncSession, user_ids: list[int]) -> dict[int, tuple[int, int]]:
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(
                UserConnectionState.user_id,
                UserConnectionState.node_streak,
                UserConnectionState.at_once_streak,
            ).where(UserConnectionState.user_id.in_(user_ids))
        )
    ).all()
    return {user_id: (node_streak or 0, at_once_streak or 0) for user_id, node_streak, at_once_streak in rows}


async def exempt_user_ids(db: AsyncSession, user_ids: list[int]) -> set[int]:
    if not user_ids:
        return set()
    rows = (
        await db.execute(
            select(UserConnectionLimit.user_id).where(
                UserConnectionLimit.user_id.in_(user_ids), UserConnectionLimit.exempt.is_(True)
            )
        )
    ).scalars().all()
    return set(rows)


async def collect_live_ips(
    users: list[User],
    by_node: dict[int, NodeActivity],
    concurrency: int = 12,
    deadline_seconds: float = 45.0,
) -> tuple[dict[int, dict[str, int]], dict[int, dict[int, int]]]:
    healthy = {node_id: node for node_id, node in await node_manager.get_healthy_nodes()}
    result: dict[int, dict[str, int]] = {}
    live_nodes: dict[int, dict[int, int]] = {}
    if not healthy:
        logger.warning("no healthy nodes; skipping address collection")
        return result, live_nodes

    semaphore = asyncio.Semaphore(concurrency)
    started = time.monotonic()
    timed_out = False

    async def for_user(user: User) -> tuple[int, dict[str, int], dict[int, int]]:
        nonlocal timed_out
        async with semaphore:
            if time.monotonic() - started > deadline_seconds:
                timed_out = True
                return user.id, {}, {}
            seen: dict[str, int] = {}
            per_node: dict[int, int] = {}
            for node_id in (by_node.get(user.id) or NodeActivity()).touched:
                node = healthy.get(node_id)
                if node is None:
                    continue
                try:
                    response = await node.get_user_online_ip_list(str(user.id), timeout=10)
                except Exception as exc:
                    logger.debug("node %s did not answer for user %s: %s", node_id, user.id, exc)
                    continue
                if response is None:
                    continue
                for ip_text, last_seen in (response.ips or {}).items():
                    if last_seen > seen.get(ip_text, 0):
                        seen[ip_text] = last_seen
                    if last_seen > per_node.get(node_id, 0):
                        per_node[node_id] = last_seen
            return user.id, seen, per_node

    for user_id, seen, per_node in await asyncio.gather(*[for_user(u) for u in users]):
        result[user_id] = seen
        live_nodes[user_id] = per_node

    if timed_out:
        logger.warning(
            "address collection hit its %.0fs deadline; %d users covered this cycle",
            deadline_seconds,
            sum(1 for v in result.values() if v),
        )
    return result, live_nodes


def infrastructure_addresses(live: dict[int, dict[str, int]], settings: ConnectionLimit) -> set[str]:
    users_per_ip: dict[str, set[int]] = defaultdict(set)
    for user_id, ips in live.items():
        for ip_text in ips:
            users_per_ip[ip_text].add(user_id)
    return {ip for ip, owners in users_per_ip.items() if len(owners) >= settings.infrastructure_min_users}


def carrier_pools(live: dict[int, dict[str, int]], settings: ConnectionLimit) -> frozenset[str]:
    if not settings.carrier_pool_min_users:
        return frozenset()

    users_per_pool: dict[str, set[int]] = defaultdict(set)
    for user_id, ips in live.items():
        for ip_text in ips:
            ip = parse_ip(ip_text)
            if ip is None or not ip.is_global:
                continue
            users_per_pool[pool_key(ip, settings)].add(user_id)

    return frozenset(
        pool for pool, owners in users_per_pool.items() if len(owners) >= settings.carrier_pool_min_users
    )


async def fetch_context(
    db: AsyncSession, user_ids: list[int], window_minutes: int
) -> tuple[dict[int, dict[str, set]], dict[int, dict[str, str]]]:
    if not user_ids:
        return {}, {}

    since = datetime.now(UTC) - timedelta(minutes=window_minutes)
    rows = (
        await db.execute(
            select(
                UserSubscriptionUpdate.user_id,
                UserSubscriptionUpdate.user_agent,
                UserSubscriptionUpdate.ip,
                UserSubscriptionUpdate.hwid,
            ).where(UserSubscriptionUpdate.user_id.in_(user_ids), UserSubscriptionUpdate.created_at > since)
        )
    ).all()

    context: dict[int, dict[str, set]] = defaultdict(lambda: {"apps": set(), "hwids": set(), "ips": set()})
    for user_id, agent, ip_text, hwid in rows:
        entry = context[user_id]
        if agent:
            entry["apps"].add(agent.split("/")[0].strip()[:64] or agent[:64])
        if hwid:
            entry["hwids"].add(hwid)
        if ip_text:
            entry["ips"].add(ip_text)

    hwid_rows = (
        await db.execute(
            select(UserHWID.user_id, UserHWID.hwid, UserHWID.device_model, UserHWID.device_os).where(
                UserHWID.user_id.in_(user_ids)
            )
        )
    ).all()
    devices: dict[int, dict[str, str]] = defaultdict(dict)
    for user_id, hwid, model, os_name in hwid_rows:
        label = " ".join(part for part in (model, os_name) if part)
        if label:
            devices[user_id][hwid] = label

    return context, devices


def _in_pools(ip_text: str, settings: ConnectionLimit, pools: frozenset[str]) -> bool:
    ip = parse_ip(ip_text)
    return ip is not None and ip.is_global and pool_key(ip, settings) in pools


def assess(
    user: User,
    live_ips: dict[str, int],
    seen_by_node: dict[int, int],
    node_ids: set[int],
    activity: NodeActivity,
    infra: set[str],
    pools: frozenset[str],
    cdn_nets: list,
    context: dict,
    devices: dict[str, str],
    settings: ConnectionLimit,
    device_limit: int | None = None,
    prior_node_streak: int = 0,
    prior_at_once_streak: int = 0,
    node_labels: dict[int, str] | None = None,
) -> Observation:
    obs = Observation(user_id=user.id, username=user.username, node_count=len(node_ids))
    obs.node_streak = prior_node_streak + 1 if obs.node_count > 1 else 0

    real_groups: dict[str, int] = {}
    cdn_seen: list[str] = []
    infra_seen: list[str] = []
    skipped: list[str] = []

    for ip_text, last_seen in live_ips.items():
        if ip_text in infra:
            infra_seen.append(ip_text)
            continue
        ip = parse_ip(ip_text)
        if ip is None:
            skipped.append(ip_text)
            continue
        if not ip.is_global:
            infra_seen.append(ip_text)
            continue
        if any(ip in net for net in cdn_nets):
            cdn_seen.append(ip_text)
            continue
        key = group_key(ip, settings, pools)
        if last_seen > real_groups.get(key, 0):
            real_groups[key] = last_seen

    pools_in_play = {key for key in real_groups if key in pools}
    pooled_addresses = sum(1 for ip_text in live_ips if _in_pools(ip_text, settings, pools))

    newest = max(real_groups.values(), default=0)
    concurrent_groups = {
        key: seen
        for key, seen in real_groups.items()
        if newest - seen <= settings.concurrency_window_seconds
    }

    address_sources = len(concurrent_groups) + (1 if cdn_seen else 0)
    obs.address_sources = address_sources

    node_labels = node_labels or {}

    apps = context.get("apps") or set()
    hwids = context.get("hwids") or set()
    obs.app_count = len(apps)
    obs.hwid_count = len(hwids)

    known_models = {devices[h].strip().lower() for h in hwids if devices.get(h)}
    if settings.count_fetched_devices:
        hwid_devices = len(hwids)
    else:
        hwid_devices = max(len(known_models), 1 if hwids else 0)
    obs.hwid_devices = hwid_devices

    newest_node = max(seen_by_node.values(), default=0)
    nodes_now = {
        node_id
        for node_id, last_seen in seen_by_node.items()
        if newest_node - last_seen <= settings.concurrency_window_seconds and node_id in activity.used
    }
    at_once = len(nodes_now) if len(nodes_now) > 1 else 0
    at_once_traffic = {node_id: activity.traffic.get(node_id, activity.concurrent_nodes.get(node_id, 0)) for node_id in nodes_now}
    obs.at_once = at_once

    obs.at_once_streak = prior_at_once_streak + 1 if at_once > 1 else 0
    at_once_counted = at_once if obs.at_once_streak >= settings.persistence_cycles else 0

    obs.devices = max(address_sources, at_once_counted, hwid_devices)

    obs.details = {
        "real_groups": sorted(concurrent_groups),
        "earlier_groups": sorted(set(real_groups) - set(concurrent_groups)),
        "cdn_addresses": sorted(cdn_seen),
        "infrastructure_addresses": sorted(infra_seen),
        "apps": sorted(apps),
        "devices": sorted(f"{devices[h]} - {h}" if devices.get(h) else h for h in hwids),
        "nodes": sorted(node_ids),
        "nodes_touched": sorted(activity.touched),
        "nodes_at_once": sorted(nodes_now) if at_once else [],
        "nodes_with_traffic_in_one_bucket": sorted(activity.concurrent_nodes),
        "node_traffic": {
            node_labels.get(node_id, str(node_id)): activity.traffic.get(node_id, 0)
            for node_id in sorted(activity.used)
        },
    }

    concurrent = len(concurrent_groups) > 1
    if len(real_groups) > 1:
        times = sorted(real_groups.values())
        obs.details["spread_seconds"] = int(times[-1] - times[0])

    distinct_networks = {
        g
        if g in pools
        else ((g.split(".")[0] + "." + g.split(".")[1]) if ":" not in g else g.split(":")[0])
        for g in concurrent_groups
    }

    limit = device_limit if device_limit is not None else settings.device_limit
    obs.limit_applied = limit
    warn_at = settings.warn_at_devices if device_limit is None else max(1, limit)

    if obs.devices > limit:
        obs.verdict = OVER_LIMIT
    elif obs.devices >= warn_at:
        obs.verdict = AT_LIMIT
    else:
        obs.verdict = WITHIN_LIMIT

    obs.reasons = _reasons(
        obs, concurrent_groups, cdn_seen, infra_seen, concurrent, distinct_networks, apps, devices, hwids,
        settings.persistence_cycles,
        known_models=known_models,
        pooled=pooled_addresses,
        pools_used=pools_in_play,
        at_once=at_once,
        at_once_counted=at_once_counted,
        at_once_streak=obs.at_once_streak,
        at_once_nodes=at_once_traffic,
        node_labels=node_labels,
        node_traffic=activity.traffic,
        used_nodes=activity.used,
        node_min_traffic_kb=settings.node_min_traffic_kb,
    )
    return obs


def _mb(byte_count: int) -> str:
    if byte_count >= 1048576:
        return f"{byte_count / 1048576:.1f}MB"
    return f"{max(1, byte_count // 1024)}KB"


def _reasons(
    obs, real_groups, cdn_seen, infra_seen, concurrent, networks, apps, devices, hwids, persistence_cycles,
    known_models, pooled, pools_used, at_once, at_once_counted, at_once_streak, at_once_nodes,
    node_labels, node_traffic, used_nodes, node_min_traffic_kb
) -> list[dict]:
    out: list[dict] = [{"code": "devices", "count": obs.devices}]

    if obs.limit_applied:
        out.append({"code": "allowance", "count": obs.limit_applied})
    if real_groups:
        out.append({"code": "networks", "count": len(real_groups), "items": sorted(real_groups)[:4]})
    if cdn_seen:
        out.append({"code": "cdn", "count": len(cdn_seen)})
    if pooled:
        out.append({"code": "carrier_pool", "count": pooled, "items": sorted(pools_used)[:3]})
    if infra_seen:
        out.append({"code": "infrastructure", "count": len(infra_seen)})

    earlier = obs.details.get("earlier_groups") or []
    if earlier:
        out.append({"code": "earlier", "count": len(earlier)})
    if concurrent:
        out.append({"code": "concurrent", "count": len(real_groups)})
    if len(networks) > 1:
        out.append({"code": "unrelated_networks", "count": len(networks)})
    if len(apps) > 1:
        out.append({"code": "apps", "count": len(apps), "items": sorted(apps)[:3]})
    if hwids:
        labels = sorted(f"{devices[h]} - {h}" if devices.get(h) else h for h in hwids)
        out.append({"code": "hardware_ids", "count": len(hwids), "items": labels[:4]})
        out.append({
            "code": "hwid_by_model",
            "count": obs.hwid_devices,
            "items": sorted(known_models)[:4],
        })
    if cdn_seen and not hwids:
        out.append({"code": "cdn_without_hwid"})
    if at_once > 1:
        node_items = [f"{node_labels.get(nid, str(nid))}: {_mb(t)}" for nid, t in sorted(at_once_nodes.items())][:4]
        if at_once_counted:
            out.append({"code": "nodes_at_once", "count": at_once, "items": node_items})
        else:
            out.append({
                "code": "nodes_at_once_pending",
                "count": at_once,
                "cycles": at_once_streak,
                "items": node_items,
            })
    if node_traffic:
        floor = node_min_traffic_kb * 1024
        items = []
        for nid, traffic in sorted(node_traffic.items(), key=lambda kv: kv[1], reverse=True):
            label = f"{node_labels.get(nid, str(nid))}: {_mb(traffic)}"
            if traffic < floor:
                label += " *"
            items.append(label)
        out.append({
            "code": "node_traffic",
            "count": len(node_traffic),
            "counted": len(used_nodes),
            "items": items[:8],
        })
    if obs.node_count > 1 and obs.node_streak >= persistence_cycles:
        out.append({"code": "nodes", "count": obs.node_count, "cycles": obs.node_streak})

    return out


async def run_assessment(db: AsyncSession, settings: ConnectionLimit) -> list[Observation]:
    users = await candidate_users(db, settings)
    if not users:
        return []

    user_ids = [u.id for u in users]
    exempt = await exempt_user_ids(db, user_ids)
    users = [u for u in users if u.id not in exempt]
    user_ids = [u.id for u in users]
    if not users:
        return []

    by_node = await nodes_per_user(db, settings, user_ids)
    await db.commit()
    live, live_nodes = await collect_live_ips(users, by_node)
    infra = infrastructure_addresses(live, settings)
    pools = carrier_pools(live, settings)
    if pools:
        logger.debug("treating %d block(s) as carrier pools", len(pools))
    if infra:
        logger.debug("treating %d address(es) as shared infrastructure", len(infra))

    context, devices = await fetch_context(db, user_ids, settings.device_window_minutes)
    overrides = await per_user_limits(db, user_ids)
    node_streaks = await prior_node_streaks(db, user_ids)
    node_labels = await node_label_map(db)
    cdn_nets = _networks(settings.cdn_ranges or DEFAULT_CDN_RANGES)

    return [
        assess(
            user,
            live.get(user.id) or {},
            live_nodes.get(user.id) or {},
            (by_node.get(user.id) or NodeActivity()).used,
            by_node.get(user.id) or NodeActivity(),
            infra,
            pools,
            cdn_nets,
            context.get(user.id) or {},
            devices.get(user.id) or {},
            settings,
            node_labels=node_labels,
            device_limit=overrides.get(user.id),
            prior_node_streak=node_streaks.get(user.id, (0, 0))[0],
            prior_at_once_streak=node_streaks.get(user.id, (0, 0))[1],
        )
        for user in users
    ]


async def node_label_map(db: AsyncSession) -> dict[int, str]:
    rows = (await db.execute(select(Node.id, Node.name, Node.address))).all()
    out: dict[int, str] = {}
    for node_id, name, address in rows:
        if name and address and name != address:
            out[node_id] = f"{name} ({address})"
        else:
            out[node_id] = address or name or str(node_id)
    return out


_provider_cache: dict[str, dict[str, str | None]] = {}
_PROVIDER_CACHE_LIMIT = 5000


async def lookup_providers(addresses: list[str]) -> dict[str, dict[str, str | None]]:
    import json
    import urllib.request

    out: dict[str, dict[str, str | None]] = {}
    to_fetch: list[str] = []

    cdn_nets = _networks(DEFAULT_CDN_RANGES)

    for address in addresses:
        if address in _provider_cache:
            out[address] = _provider_cache[address]
            continue

        ip = parse_ip(address)
        if ip is None:
            out[address] = {"provider": None, "country": None}
            continue
        if not ip.is_global:
            out[address] = {"provider": "private / tunnel", "country": None}
            continue
        if ip.version == 4 and any(ip in net for net in cdn_nets):
            out[address] = {"provider": "CDN", "country": None}
            continue
        to_fetch.append(address)

    if not to_fetch:
        return out

    loop = asyncio.get_running_loop()

    def fetch(address: str) -> dict[str, str | None]:
        try:
            request = urllib.request.Request(
                f"http://ip-api.com/json/{address}?fields=status,country,isp,org",
                headers={"User-Agent": "pasarguard-panel"},
            )
            with urllib.request.urlopen(request, timeout=6) as response:
                data = json.loads(response.read() or b"{}")
            if data.get("status") != "success":
                return {"provider": None, "country": None}
            return {"provider": data.get("isp") or data.get("org"), "country": data.get("country")}
        except Exception:
            return {"provider": None, "country": None}

    semaphore = asyncio.Semaphore(5)

    async def one(address: str):
        async with semaphore:
            result = await loop.run_in_executor(None, fetch, address)
            if len(_provider_cache) < _PROVIDER_CACHE_LIMIT:
                _provider_cache[address] = result
            return address, result

    for address, result in await asyncio.gather(*[one(a) for a in to_fetch[:40]]):
        out[address] = result

    return out
