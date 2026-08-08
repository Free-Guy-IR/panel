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

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    NodeUserUsage,
    User,
    UserConnectionLimit,
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


def group_key(ip, settings: ConnectionLimit) -> str:
    """Collapse an address to the unit worth counting as one place."""
    prefix = settings.ipv4_group_prefix if ip.version == 4 else settings.ipv6_group_prefix
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
    app_count: int = 0
    verdict: str = WITHIN_LIMIT
    reasons: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


async def candidate_users(db: AsyncSession, settings: ConnectionLimit) -> list[User]:
    """Users recent enough to be worth asking the nodes about."""
    since = datetime.now(UTC) - timedelta(seconds=settings.online_window_seconds)
    stmt = select(User).where(User.online_at.is_not(None), User.online_at > since, User.status == "active")

    if settings.apply_to_admin_ids:
        stmt = stmt.where(User.admin_id.in_(settings.apply_to_admin_ids))

    if settings.apply_to_group_ids:
        from app.db.models import users_groups_association

        stmt = stmt.where(
            User.id.in_(
                select(users_groups_association.c.user_id).where(
                    users_groups_association.c.groups_id.in_(settings.apply_to_group_ids)
                )
            )
        )

    return list((await db.execute(stmt)).scalars().all())


async def nodes_per_user(db: AsyncSession, settings: ConnectionLimit, user_ids: list[int]) -> dict[int, set[int]]:
    """Which nodes each user has been carried on lately.

    Without this every user would have to be looked up on every node, which on
    this panel is ~19 calls each instead of the ~3 they actually need.
    """
    if not user_ids:
        return {}

    since = datetime.now(UTC) - timedelta(minutes=settings.node_window_minutes)
    rows = (
        await db.execute(
            select(distinct(NodeUserUsage.user_id), NodeUserUsage.node_id).where(
                NodeUserUsage.user_id.in_(user_ids),
                NodeUserUsage.created_at > since,
                NodeUserUsage.node_id.is_not(None),
                NodeUserUsage.used_traffic > 0,
            )
        )
    ).all()

    out: dict[int, set[int]] = {}
    for user_id, node_id in rows:
        out.setdefault(user_id, set()).add(node_id)
    return out


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
    by_node: dict[int, set[int]],
    concurrency: int = 12,
    deadline_seconds: float = 45.0,
) -> dict[int, dict[str, int]]:
    """Ask each user's own nodes for their live addresses and last-seen times.

    Users are polled concurrently: done one at a time this is thousands of
    sequential round trips and cannot finish inside a cycle.

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
            for node_id in by_node.get(user.id, set()):
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
        label = " ".join(part for part in (model, os_name) if part) or hwid[:12]
        devices[user_id][hwid] = label

    return context, devices


def assess(
    user: User,
    live_ips: dict[str, int],
    node_ids: set[int],
    infra: set[str],
    cdn_nets: list,
    context: dict,
    devices: dict[str, str],
    settings: ConnectionLimit,
) -> Observation:
    """Turn one user's raw sightings into a verdict with its reasons."""
    obs = Observation(user_id=user.id, username=user.username, node_count=len(node_ids))

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
        key = group_key(ip, settings)
        if last_seen > real_groups.get(key, 0):
            real_groups[key] = last_seen

    # Only what was live at the same moment counts. An address seen ten
    # minutes ago is the same person earlier, not another person now.
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

    # Each signal is a floor and each is blind to what the other sees: hardware
    # ids find devices sharing one address, addresses find devices whose app
    # reports no hardware id. The larger is the better estimate.
    obs.devices = max(address_sources, len(hwids))

    obs.details = {
        "real_groups": sorted(concurrent_groups),
        "earlier_groups": sorted(set(real_groups) - set(concurrent_groups)),
        "cdn_addresses": sorted(cdn_seen),
        "infrastructure_addresses": sorted(infra_seen),
        "apps": sorted(apps),
        "devices": sorted({devices.get(h, h[:12]) for h in hwids}),
        "nodes": sorted(node_ids),
    }

    concurrent = len(concurrent_groups) > 1
    if len(real_groups) > 1:
        times = sorted(real_groups.values())
        obs.details["spread_seconds"] = int(times[-1] - times[0])

    # Two addresses inside one carrier block are usually one moving phone, so
    # unrelated networks are what actually suggest separate people.
    distinct_networks = {
        (g.split(".")[0] + "." + g.split(".")[1]) if ":" not in g else g.split(":")[0]
        for g in concurrent_groups
    }

    # Purely a comparison against the configured allowance. No inference about
    # whether the devices belong to one person or several.
    if obs.devices > settings.device_limit:
        obs.verdict = OVER_LIMIT
    elif obs.devices >= settings.warn_at_devices:
        obs.verdict = AT_LIMIT
    else:
        obs.verdict = WITHIN_LIMIT

    obs.reasons = _reasons(
        obs, concurrent_groups, cdn_seen, infra_seen, concurrent, distinct_networks, apps, devices, hwids
    )
    return obs


def _reasons(obs, real_groups, cdn_seen, infra_seen, concurrent, networks, apps, devices, hwids) -> list[str]:
    """Plain statements of what was seen, so the number can be checked."""
    out = [f"{obs.devices} device(s) estimated"]
    if real_groups:
        out.append(f"{len(real_groups)} distinct network(s): {', '.join(sorted(real_groups)[:4])}")
    if cdn_seen:
        out.append(f"{len(cdn_seen)} CDN address(es), counted as one source")
    if infra_seen:
        out.append(f"{len(infra_seen)} shared/tunnel address(es), not counted")
    earlier = obs.details.get("earlier_groups") or []
    if earlier:
        out.append(f"{len(earlier)} further network(s) seen earlier, not at the same time - not counted")
    if concurrent:
        out.append(f"{len(real_groups)} of them live at the same moment")
    if len(networks) > 1:
        out.append(f"{len(networks)} unrelated networks")
    if len(apps) > 1:
        out.append(f"{len(apps)} different apps: {', '.join(sorted(apps)[:3])}")
    if hwids:
        labels = sorted({devices.get(h, h[:12]) for h in hwids})
        out.append(f"{len(hwids)} hardware id(s) reported: {', '.join(labels[:3])}")
    if cdn_seen and not hwids:
        out.append("behind a CDN with no hardware id reported - several devices would look like one")
    if obs.node_count > 1:
        out.append(f"on {obs.node_count} nodes (may just be an app that probes every server)")
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
    if infra:
        logger.debug("treating %d address(es) as shared infrastructure", len(infra))

    context, devices = await fetch_context(db, user_ids, settings.node_window_minutes)
    cdn_nets = _networks(settings.cdn_ranges or DEFAULT_CDN_RANGES)

    return [
        assess(
            user,
            live.get(user.id) or {},
            by_node.get(user.id, set()),
            infra,
            cdn_nets,
            context.get(user.id) or {},
            devices.get(user.id) or {},
            settings,
        )
        for user in users
    ]
