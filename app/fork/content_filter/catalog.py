import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).parent
SERVICES_FILE = HERE / "services.json"
PROTECTION_FILE = HERE / "protection_lists.json"
LISTS_INDEX = HERE / "pgfilter_index.json"
DAT_FILE = "pgfilter.dat"

GEOSITE_GROUPS: tuple[tuple[str, str, int], ...] = (
    ("adult", "category-porn", 6635),
    ("ads", "category-ads-all", 170624),
)

GROUP_ORDER = (
    "adult",
    "social_network",
    "messenger",
    "streaming",
    "gaming",
    "gambling",
    "dating",
    "ai",
    "shopping",
    "hosting",
    "cdn",
    "software",
    "privacy",
    "ads",
)


@dataclass(frozen=True)
class Entry:
    key: str
    label: str
    geosite: str | None
    domains: tuple[str, ...]
    external: str | None = None
    declared_size: int = 0

    @property
    def size(self) -> int:
        return self.declared_size or len(self.domains)


@dataclass(frozen=True)
class ListGroup:
    key: str
    entries: tuple[Entry, ...]

    @property
    def size(self) -> int:
        return sum(e.size for e in self.entries)


@dataclass(frozen=True)
class Group:
    key: str
    geosite: str | None
    size: int
    entries: tuple[Entry, ...]


@lru_cache(maxsize=1)
def _load() -> tuple[tuple[Group, ...], tuple[Entry, ...]]:
    services = json.loads(SERVICES_FILE.read_text(encoding="utf-8"))["services"]
    buckets: dict[str, list[Entry]] = {}
    for service in services:
        entry = Entry(
            key=service["id"],
            label=service["name"],
            geosite=service.get("geosite"),
            domains=tuple(service.get("domains") or ()),
        )
        buckets.setdefault(service.get("group") or "other", []).append(entry)

    groups: list[Group] = []
    for key, geosite, size in GEOSITE_GROUPS:
        groups.append(Group(key=key, geosite=geosite, size=size, entries=()))
    for key, entries in buckets.items():
        ordered = tuple(sorted(entries, key=lambda e: e.label.lower()))
        groups.append(
            Group(key=key, geosite=None, size=sum(e.size for e in ordered), entries=ordered)
        )

    order = {name: index for index, name in enumerate(GROUP_ORDER)}
    groups.sort(key=lambda g: (order.get(g.key, len(order)), g.key))

    protection = tuple(
        Entry(key=item["key"], label=item["label"], geosite=None, domains=tuple(item["domains"]))
        for item in json.loads(PROTECTION_FILE.read_text(encoding="utf-8"))["lists"]
    )
    return tuple(groups), protection


@lru_cache(maxsize=1)
def curated_lists() -> tuple[ListGroup, ...]:
    if not LISTS_INDEX.exists():
        return ()
    data = json.loads(LISTS_INDEX.read_text(encoding="utf-8"))
    buckets: dict[str, list[Entry]] = {}
    for item in data.get("lists", []):
        entry = Entry(
            key=item["key"],
            label=item["label"],
            geosite=None,
            domains=(),
            external=f"{data.get('file', DAT_FILE)}:{item['key']}",
            declared_size=int(item.get("domains") or 0),
        )
        buckets.setdefault(item.get("group") or "other", []).append(entry)
    order = ("security", "bypass", "ads", "devices", "content", "regional")
    out = [
        ListGroup(key=key, entries=tuple(sorted(buckets[key], key=lambda e: -e.size)))
        for key in order
        if key in buckets
    ]
    out += [ListGroup(key=k, entries=tuple(v)) for k, v in buckets.items() if k not in order]
    return tuple(out)


def dat_sha256() -> str | None:
    if not LISTS_INDEX.exists():
        return None
    return json.loads(LISTS_INDEX.read_text(encoding="utf-8")).get("sha256")


def groups() -> tuple[Group, ...]:
    return _load()[0]


def protection_lists() -> tuple[Entry, ...]:
    return _load()[1]


@lru_cache(maxsize=1)
def _index() -> dict[str, Entry | Group]:
    found: dict[str, Entry | Group] = {}
    for group in groups():
        found[group.key] = group
        for entry in group.entries:
            found[entry.key] = entry
    for entry in protection_lists():
        found[entry.key] = entry
    for group in curated_lists():
        for entry in group.entries:
            found[entry.key] = entry
    return found


def selectable_keys() -> frozenset[str]:
    return frozenset(_index())


LEGACY_KEYS = {
    "social": "social_network",
    "games": "gaming",
    "ads_all": "ads",
    "disneyplus": "disney",
    "epicgames": "epic_games",
    "riot": "riot_games",
    "blizzard": "blizzard_entertainment",
    "ea": "electronic_arts",
    "xbox": "xboxlive",
}


def normalise(keys: list[str]) -> list[str]:
    known = selectable_keys()
    out: list[str] = []
    for key in keys:
        candidate = key if key in known else LEGACY_KEYS.get(key, key)
        if candidate in known and candidate not in out:
            out.append(candidate)
    return sorted(out)


def unknown_keys(keys: list[str]) -> list[str]:
    known = selectable_keys()
    return sorted({k for k in keys if k not in known and LEGACY_KEYS.get(k) not in known})


def size_of(key: str) -> int:
    found = _index().get(key)
    if found is None:
        return 0
    return found.size


def expand(keys: list[str]) -> tuple[list[str], list[str], list[str]]:
    selected = set(keys)
    geosites: list[str] = []
    domains: list[str] = []
    seen_geo: set[str] = set()
    seen_dom: set[str] = set()

    def take_geo(name: str) -> None:
        if name not in seen_geo:
            seen_geo.add(name)
            geosites.append(name)

    externals: list[str] = []
    seen_ext: set[str] = set()

    def take_entry(entry: Entry) -> None:
        if entry.geosite:
            take_geo(entry.geosite)
        if entry.external and entry.external not in seen_ext:
            seen_ext.add(entry.external)
            externals.append(entry.external)
        for domain in entry.domains:
            if domain not in seen_dom:
                seen_dom.add(domain)
                domains.append(domain)

    for group in groups():
        whole = group.key in selected
        if whole and group.geosite:
            take_geo(group.geosite)
            continue
        for entry in group.entries:
            if whole or entry.key in selected:
                take_entry(entry)

    for entry in protection_lists():
        if entry.key in selected:
            take_entry(entry)

    for group in curated_lists():
        for entry in group.entries:
            if entry.key in selected:
                take_entry(entry)

    return geosites, domains, externals


def catalog_payload() -> dict:
    return {
        "groups": [
            {
                "key": group.key,
                "geosite": group.geosite,
                "domains": group.size,
                "services": [
                    {"key": e.key, "label": e.label, "geosite": e.geosite, "domains": e.size}
                    for e in group.entries
                ],
            }
            for group in groups()
        ],
        "protection": [
            {"key": e.key, "label": e.label, "domains": e.size} for e in protection_lists()
        ],
        "lists": [
            {
                "key": group.key,
                "domains": group.size,
                "entries": [{"key": e.key, "label": e.label, "domains": e.size} for e in group.entries],
            }
            for group in curated_lists()
        ],
    }
