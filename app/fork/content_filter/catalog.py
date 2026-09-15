from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    geosite: str
    domains: int


@dataclass(frozen=True)
class CatalogGroup:
    key: str
    geosite: str | None
    domains: int
    entries: tuple[CatalogEntry, ...]


CATALOG: tuple[CatalogGroup, ...] = (
    CatalogGroup(
        key="adult",
        geosite="category-porn",
        domains=6635,
        entries=(
            CatalogEntry("pornhub", "pornhub", 9),
            CatalogEntry("xvideos", "xvideos", 5),
        ),
    ),
    CatalogGroup(
        key="social",
        geosite="category-social-media-!cn",
        domains=598,
        entries=(
            CatalogEntry("instagram", "instagram", 74),
            CatalogEntry("telegram", "telegram", 20),
            CatalogEntry("whatsapp", "whatsapp", 13),
            CatalogEntry("facebook", "facebook", 397),
            CatalogEntry("meta", "meta", 553),
            CatalogEntry("tiktok", "tiktok", 37),
            CatalogEntry("youtube", "youtube", 178),
            CatalogEntry("twitter", "twitter", 24),
            CatalogEntry("x", "x", 26),
            CatalogEntry("threads", "threads", 2),
            CatalogEntry("reddit", "reddit", 12),
            CatalogEntry("pinterest", "pinterest", 52),
            CatalogEntry("linkedin", "linkedin", 12),
            CatalogEntry("discord", "discord", 28),
        ),
    ),
    CatalogGroup(
        key="games",
        geosite="category-games",
        domains=1011,
        entries=(
            CatalogEntry("steam", "steam", 54),
            CatalogEntry("epicgames", "epicgames", 31),
            CatalogEntry("riot", "riot", 54),
            CatalogEntry("blizzard", "blizzard", 23),
            CatalogEntry("ea", "ea", 165),
            CatalogEntry("origin", "origin", 9),
            CatalogEntry("ubisoft", "ubisoft", 32),
            CatalogEntry("roblox", "roblox", 48),
            CatalogEntry("pubg", "pubg", 4),
            CatalogEntry("garena", "garena", 16),
            CatalogEntry("nintendo", "nintendo", 124),
            CatalogEntry("playstation", "playstation", 4),
            CatalogEntry("xbox", "xbox", 45),
            CatalogEntry("twitch", "twitch", 32),
        ),
    ),
    CatalogGroup(
        key="streaming",
        geosite=None,
        domains=348,
        entries=(
            CatalogEntry("netflix", "netflix", 28),
            CatalogEntry("disney", "disney", 157),
            CatalogEntry("hulu", "hulu", 48),
            CatalogEntry("hbo", "hbo", 63),
            CatalogEntry("primevideo", "primevideo", 23),
            CatalogEntry("spotify", "spotify", 26),
            CatalogEntry("soundcloud", "soundcloud", 3),
        ),
    ),
    CatalogGroup(
        key="ads",
        geosite="category-ads",
        domains=639,
        entries=(CatalogEntry("ads_all", "category-ads-all", 170624),),
    ),
)

_BY_KEY: dict[str, str] = {}
_DOMAINS: dict[str, int] = {}
for _group in CATALOG:
    if _group.geosite:
        _BY_KEY[_group.key] = _group.geosite
        _DOMAINS[_group.key] = _group.domains
    for _entry in _group.entries:
        _BY_KEY[_entry.key] = _entry.geosite
        _DOMAINS[_entry.key] = _entry.domains

SELECTABLE_KEYS = frozenset(_BY_KEY)


def geosite_for(key: str) -> str | None:
    return _BY_KEY.get(key)


def domains_for(key: str) -> int:
    return _DOMAINS.get(key, 0)


def unknown_keys(keys: list[str]) -> list[str]:
    return sorted(k for k in keys if k not in SELECTABLE_KEYS)


def expand(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    selected = set(keys)

    def take(geosite: str) -> None:
        if geosite not in seen:
            seen.add(geosite)
            ordered.append(geosite)

    for group in CATALOG:
        whole_group = group.key in selected
        if whole_group and group.geosite:
            take(group.geosite)
            continue
        for entry in group.entries:
            if whole_group or entry.key in selected:
                take(entry.geosite)
    return ordered


def catalog_payload() -> list[dict]:
    return [
        {
            "key": group.key,
            "geosite": group.geosite,
            "domains": group.domains,
            "services": [
                {"key": entry.key, "geosite": entry.geosite, "domains": entry.domains} for entry in group.entries
            ],
        }
        for group in CATALOG
    ]
