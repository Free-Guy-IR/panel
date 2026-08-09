"""Work out how many people are using one subscription at once.

Each user's live addresses come straight from the nodes, so no log parsing is
involved. The hard part is not counting addresses - it is avoiding mistaking
one person for several. Measured against real traffic on a panel with ~1,600
concurrent users, the things that fool a naive count are:

  * a phone moving between towers, which changes address inside one carrier
    block and looks like a second person;
  * a CDN, which presents one person as several edge addresses (Cloudflare and
    Fastly both, so a hardcoded list of one provider is not enough);
  * a tunnel or NAT, which presents *everybody* as one address - seen here as a
    single address attributed to 31 different users;
  * a placeholder address some inbounds report, seen attributed to 23 users at
    once, which a naive count reads as 23 people sharing.

So addresses are grouped by prefix, CDN addresses collapse to one source, and
anything currently attributed to many users is treated as infrastructure and
ignored. Infrastructure is detected rather than configured, which is what makes
it cover providers nobody thought to list.

Where the address alone cannot separate two people - both behind the same CDN,
for instance - the subscription-fetch record can: it ties each address to the
app that fetched with it and, three quarters of the time here, to a hardware
id. Two different devices are two people regardless of how their traffic is
routed.

Nothing in this module acts on a user. It reports what it sees.
"""

import asyncio
import ipaddress
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
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

# Published ranges for the two CDNs in use here. Editable in settings, because
# the next one to be added will not be in this list.
DEFAULT_CDN_RANGES = [
    # Cloudflare
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
    # Fastly
    "23.235.32.0/20", "43.249.72.0/22", "103.244.50.0/24", "103.245.222.0/23",
    "103.245.224.0/24", "104.156.80.0/20", "140.248.64.0/18", "140.248.128.0/17",
    "146.75.0.0/16", "151.101.0.0/16", "157.52.64.0/18", "167.82.0.0/17",
    "167.82.128.0/20", "167.82.160.0/20", "167.82.224.0/20", "172.111.64.0/18",
    "185.31.16.0/22", "199.27.72.0/21", "199.232.0.0/16",
    "2a04:4e40::/32", "2a04:4e42::/32",
]

# What the count means, not a claim about who is behind it.
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
    """Collapse an address to the unit worth counting as one place.

    Normally that is the /24 it sits in, which keeps a phone moving between
    towers inside one block from reading as several people. Inside a carrier
    pool it is the whole pool: the carrier hands out addresses from across it
    to a single phone, so the /24 says nothing about who is on the other end.
    """
    if pools:
        pool = pool_key(ip, settings)
        if pool in pools:
            return pool
    prefix = settings.ipv4_group_prefix if ip.version == 4 else settings.ipv6_group_prefix
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


def pool_key(ip, settings: ConnectionLimit) -> str:
    """The block an address would belong to if it were part of a carrier pool."""
    prefix = settings.carrier_pool_prefix if ip.version == 4 else settings.carrier_pool_prefix_v6
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False))


@dataclass
class Observation:
    """What was seen for one user in one cycle, and what it means."""

    user_id: int
    username: str
    # Estimated devices connected at once - the number the badge shows.
    devices: int = 0
    # The two floors it was taken from, kept so the popover can explain it.
    address_sources: int = 0
    hwid_count: int = 0
    node_count: int = 0
    # How many checks in a row the user has been on more than one node.
    node_streak: int = 0
    app_count: int = 0
    verdict: str = WITHIN_LIMIT
    # How many cycles running this verdict has held. Filled in by the job,
    # which is the only thing that can see across cycles, and read by
    # enforcement - one cycle proves nothing.
    streak: int = 0
    # The allowance this user was actually judged against - their own where
    # one is set, otherwise the global default.
    limit_applied: int = 0
    reasons: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)


def scope_conditions(settings: ConnectionLimit) -> list:
    """Who the settings cover, as conditions on the users table.

    Separate from being online, which decides when a covered user is worth
    asking about rather than whether they are covered at all.
    """
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
    """Users recent enough to be worth asking the nodes about."""
    since = datetime.now(UTC) - timedelta(seconds=settings.online_window_seconds)
    stmt = select(User).where(User.online_at.is_not(None), User.online_at > since, *scope_conditions(settings))
    return list((await db.execute(stmt)).scalars().all())


async def prune_out_of_scope(db: AsyncSession, settings: ConnectionLimit) -> int:
    """Drop the rows for users the settings have stopped covering.

    Nothing rewrites a row once its user is out of scope, so without this the
    review list keeps showing whoever was in the group that was selected last
    week, frozen at whatever their final check said.
    """
    covered = select(User.id).where(*scope_conditions(settings))
    exempt = select(UserConnectionLimit.user_id).where(UserConnectionLimit.exempt.is_(True))

    result = await db.execute(
        delete(UserConnectionState).where(
            or_(
                UserConnectionState.user_id.not_in(covered),
                UserConnectionState.user_id.in_(exempt),
            )
        )
    )
    return result.rowcount or 0


async def nodes_per_user(
    db: AsyncSession, settings: ConnectionLimit, user_ids: list[int]
) -> dict[int, dict[int, int]]:
    """Which nodes each user has been carried on lately, and how much each carried.

    Without this every user would have to be looked up on every node, which on
    this panel is ~19 calls each instead of the ~3 they actually need.
    """
    if not user_ids:
        return {}

    since = datetime.now(UTC) - timedelta(minutes=settings.node_window_minutes)
    rows = (
        await db.execute(
            select(
                NodeUserUsage.user_id,
                NodeUserUsage.node_id,
                func.sum(NodeUserUsage.used_traffic),
            )
            .where(
                NodeUserUsage.user_id.in_(user_ids),
                NodeUserUsage.created_at > since,
                NodeUserUsage.node_id.is_not(None),
                NodeUserUsage.used_traffic > 0,
            )
            .group_by(NodeUserUsage.user_id, NodeUserUsage.node_id)
        )
    ).all()

    out: dict[int, dict[int, int]] = {}
    for user_id, node_id, traffic in rows:
        out.setdefault(user_id, {})[node_id] = int(traffic or 0)
    return out


def nodes_actually_used(traffic_by_node: dict[int, int], settings: ConnectionLimit) -> set[int]:
    """The nodes that carried real traffic, not just a handshake.

    Trying every server leaves a few kilobytes on each; using one leaves
    megabytes. A threshold of zero counts every node the user touched.
    """
    floor = settings.node_min_traffic_kb * 1024
    return {node_id for node_id, traffic in traffic_by_node.items() if traffic >= floor}


async def per_user_limits(db: AsyncSession, user_ids: list[int]) -> dict[int, int]:
    """Allowances for the users that have their own; the rest use the default."""
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


async def prior_node_streaks(db: AsyncSession, user_ids: list[int]) -> dict[int, int]:
    """How long each user has already been on more than one node.

    The run has to survive across cycles to mean anything, and the state
    row is where it lives between them.
    """
    if not user_ids:
        return {}
    rows = (
        await db.execute(
            select(UserConnectionState.user_id, UserConnectionState.node_streak).where(
                UserConnectionState.user_id.in_(user_ids)
            )
        )
    ).all()
    return {user_id: streak or 0 for user_id, streak in rows}


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
    by_node: dict[int, dict[int, int]],
    concurrency: int = 12,
    deadline_seconds: float = 45.0,
) -> dict[int, dict[str, int]]:
    """Ask each user's own nodes for their live addresses and last-seen times.

    Users are polled concurrently: done one at a time this is thousands of
    sequential round trips and cannot finish inside a cycle.

    Every node the user touched is asked, however little it carried. A node
    that only saw a handshake is not reported as one they are on, but it can
    still be holding a live address, and missing that would undercount them.

    A node that does not answer is skipped rather than counted as zero, so an
    unreachable node cannot make a user look compliant or over the limit. If
    the deadline passes, whatever has been gathered is returned and the rest
    are left for the next cycle - a cycle that runs forever blocks every cycle
    after it.
    """
    healthy = {node_id: node for node_id, node in await node_manager.get_healthy_nodes()}
    result: dict[int, dict[str, int]] = {}
    if not healthy:
        logger.warning("no healthy nodes; skipping address collection")
        return result

    semaphore = asyncio.Semaphore(concurrency)
    started = time.monotonic()
    timed_out = False

    async def for_user(user: User) -> tuple[int, dict[str, int]]:
        nonlocal timed_out
        async with semaphore:
            if time.monotonic() - started > deadline_seconds:
                timed_out = True
                return user.id, {}
            seen: dict[str, int] = {}
            for node_id in by_node.get(user.id) or {}:
                node = healthy.get(node_id)
                if node is None:
                    continue
                try:
                    # Nodes index stats by the user's id, not their name - the
                    # panel's own IP-list endpoint does the same.
                    response = await node.get_user_online_ip_list(str(user.id), timeout=10)
                except Exception as exc:
                    logger.debug("node %s did not answer for user %s: %s", node_id, user.id, exc)
                    continue
                if response is None:
                    continue
                for ip_text, last_seen in (response.ips or {}).items():
                    # Keep the most recent sighting of each address.
                    if last_seen > seen.get(ip_text, 0):
                        seen[ip_text] = last_seen
            return user.id, seen

    for user_id, seen in await asyncio.gather(*[for_user(u) for u in users]):
        result[user_id] = seen

    if timed_out:
        logger.warning(
            "address collection hit its %.0fs deadline; %d users covered this cycle",
            deadline_seconds,
            sum(1 for v in result.values() if v),
        )
    return result


def infrastructure_addresses(live: dict[int, dict[str, int]], settings: ConnectionLimit) -> set[str]:
    """Addresses currently attributed to many users identify nobody.

    Tunnels, NATs, CDN edges under load and placeholder addresses all show up
    this way, so detecting the shape catches providers that were never listed.
    """
    users_per_ip: dict[str, set[int]] = defaultdict(set)
    for user_id, ips in live.items():
        for ip_text in ips:
            users_per_ip[ip_text].add(user_id)
    return {ip for ip, owners in users_per_ip.items() if len(owners) >= settings.infrastructure_min_users}


def carrier_pools(live: dict[int, dict[str, int]], settings: ConnectionLimit) -> frozenset[str]:
    """Blocks that many different users are inside, which makes them a pool.

    One household is one block. A block that a large slice of the panel is
    sitting in belongs to a carrier, and the addresses it hands out cannot be
    told apart by their /24.
    """
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
    """Apps, devices and hardware ids seen fetching each subscription lately.

    This is what separates two people who are both behind the same CDN, where
    the address on its own cannot.
    """
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
            # The version suffix changes on every app update; the client name
            # is the part that says anything about who is connecting.
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
    node_ids: set[int],
    node_traffic: dict[int, int],
    infra: set[str],
    pools: frozenset[str],
    cdn_nets: list,
    context: dict,
    devices: dict[str, str],
    settings: ConnectionLimit,
    device_limit: int | None = None,
    prior_node_streak: int = 0,
) -> Observation:
    """Turn one user's raw sightings into a verdict with its reasons."""
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

    # Only what was live at the same moment counts. An address seen ten
    # minutes ago is the same person earlier, not another person now.
    pools_in_play = {key for key in real_groups if key in pools}
    pooled_addresses = sum(1 for ip_text in live_ips if _in_pools(ip_text, settings, pools))

    newest = max(real_groups.values(), default=0)
    concurrent_groups = {
        key: seen
        for key, seen in real_groups.items()
        if newest - seen <= settings.concurrency_window_seconds
    }

    # A CDN hides how many devices are behind it, so it counts once. This is
    # therefore a floor on the number of devices, not the number itself.
    address_sources = len(concurrent_groups) + (1 if cdn_seen else 0)
    obs.address_sources = address_sources

    apps = context.get("apps") or set()
    hwids = context.get("hwids") or set()
    obs.app_count = len(apps)
    obs.hwid_count = len(hwids)

    # Fetching the subscription means a device holds the configuration, not
    # that it is connected, so on its own it is not evidence of a device being
    # used. Behind a CDN it is the only thing that can tell two devices apart -
    # every address collapses to one there - which is the case it was for.
    counted_fetches = address_sources > 0 and (bool(cdn_seen) or settings.count_fetched_devices)
    if address_sources == 0:
        obs.devices = 0
    elif counted_fetches:
        obs.devices = max(address_sources, len(hwids))
    else:
        obs.devices = address_sources

    obs.details = {
        "real_groups": sorted(concurrent_groups),
        "earlier_groups": sorted(set(real_groups) - set(concurrent_groups)),
        "cdn_addresses": sorted(cdn_seen),
        "infrastructure_addresses": sorted(infra_seen),
        "apps": sorted(apps),
        "devices": sorted(f"{devices[h]} - {h}" if devices.get(h) else h for h in hwids),
        "nodes": sorted(node_ids),
        "nodes_touched": sorted(node_traffic),
    }

    concurrent = len(concurrent_groups) > 1
    if len(real_groups) > 1:
        times = sorted(real_groups.values())
        obs.details["spread_seconds"] = int(times[-1] - times[0])

    # Two addresses inside one carrier block are usually one moving phone, so
    # unrelated networks are what actually suggest separate people.
    # A pool is already one network however wide it is, so it stands for
    # itself here rather than being cut into octets that mean nothing.
    distinct_networks = {
        g
        if g in pools
        else ((g.split(".")[0] + "." + g.split(".")[1]) if ":" not in g else g.split(":")[0])
        for g in concurrent_groups
    }

    # Purely a comparison against the allowance in force for this user. No
    # inference about whether the devices belong to one person or several.
    limit = device_limit if device_limit is not None else settings.device_limit
    obs.limit_applied = limit
    # Warn one below the limit when the user has their own, rather than at the
    # global warn level which may sit above their allowance entirely.
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
        counted_fetches=counted_fetches,
        pooled=pooled_addresses,
        pools_used=pools_in_play,
    )
    return obs


def _reasons(
    obs, real_groups, cdn_seen, infra_seen, concurrent, networks, apps, devices, hwids, persistence_cycles,
    counted_fetches, pooled, pools_used
) -> list[dict]:
    """What was seen, as codes the frontend renders in the reader's language.

    Values travel alongside the code rather than baked into a sentence, so the
    wording lives with the translations and a row written today reads correctly
    in a language added tomorrow.
    """
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
        # Keyed by the id, so two devices reporting the same model stay two,
        # and the id itself is shown - it is what identifies the device.
        labels = sorted(f"{devices[h]} - {h}" if devices.get(h) else h for h in hwids)
        out.append({"code": "hardware_ids", "count": len(hwids), "items": labels[:3]})
    if hwids and not counted_fetches:
        out.append({"code": "fetched_not_counted", "count": len(hwids)})
    if cdn_seen and not hwids:
        out.append({"code": "cdn_without_hwid"})
    # Held for long enough to be worth reading. Below that it is as likely
    # to be one refresh of an app that tries every server as anything else.
    if obs.node_count > 1 and obs.node_streak >= persistence_cycles:
        out.append({"code": "nodes", "count": obs.node_count, "cycles": obs.node_streak})

    return out


async def run_assessment(db: AsyncSession, settings: ConnectionLimit) -> list[Observation]:
    """One full pass: gather, judge, and hand back the observations."""
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
    live = await collect_live_ips(users, by_node)
    infra = infrastructure_addresses(live, settings)
    pools = carrier_pools(live, settings)
    if pools:
        logger.debug("treating %d block(s) as carrier pools", len(pools))
    if infra:
        logger.debug("treating %d address(es) as shared infrastructure", len(infra))

    context, devices = await fetch_context(db, user_ids, settings.node_window_minutes)
    overrides = await per_user_limits(db, user_ids)
    node_streaks = await prior_node_streaks(db, user_ids)
    cdn_nets = _networks(settings.cdn_ranges or DEFAULT_CDN_RANGES)

    return [
        assess(
            user,
            live.get(user.id) or {},
            nodes_actually_used(by_node.get(user.id) or {}, settings),
            by_node.get(user.id) or {},
            infra,
            pools,
            cdn_nets,
            context.get(user.id) or {},
            devices.get(user.id) or {},
            settings,
            overrides.get(user.id),
            node_streaks.get(user.id, 0),
        )
        for user in users
    ]


# Provider names are looked up on demand and cached for the process lifetime.
# The provider behind an address effectively never changes, and a review only
# ever asks about a handful, so this stays small and costs nothing in the
# checking loop.
_provider_cache: dict[str, dict[str, str | None]] = {}
_PROVIDER_CACHE_LIMIT = 5000


async def lookup_providers(addresses: list[str]) -> dict[str, dict[str, str | None]]:
    """Name the provider behind each address, as far as that is knowable.

    Private and CDN addresses are answered locally - there is no third party
    to ask about 10.0.0.1, and a Cloudflare range is already known. Anything
    else goes to a public lookup service, which means the address leaves this
    server; the caller is responsible for having been given permission.
    """
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

    # Bounded, and only ever the addresses on one screen.
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
