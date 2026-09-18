from datetime import datetime as dt
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.fork.content_filter.catalog import normalise

MAX_LIST_ENTRIES = 512
DOMAIN_MAX = 253
LABEL_MAX = 63
LABEL_EXTRA_CHARS = "-_"
FORBIDDEN_DOMAIN_CHARS = frozenset(":,;&#?!%~()[]{}^$|<>`\"'\\/+@ ")


def _reject(entry: str, problem: str) -> None:
    raise ValueError(f"{entry!r} cannot be used as a domain: {problem}")


def _canonical_domain(entry: str) -> str:
    lowered = entry.lower()
    if ":" in lowered:
        _reject(
            entry,
            "it carries a matcher prefix; write the plain domain instead, optionally as "
            "'=exact.example.com' for one exact name or '*.example.com' for a whole subtree",
        )
    for index, char in enumerate(lowered):
        if char.isspace() or not char.isprintable():
            _reject(entry, "it holds a space or an invisible character")
        if char in FORBIDDEN_DOMAIN_CHARS:
            _reject(entry, f"the character {char!r} cannot appear in a domain")
        if char == "*" and not (index == 0 and lowered.startswith("*.")):
            _reject(entry, "'*' is only allowed as the leading '*.' of a subtree, as in '*.example.com'")
        if char == "=" and index != 0:
            _reject(entry, "'=' is only allowed at the very start, as in '=exact.example.com'")

    prefix, body = "", lowered
    if body.startswith("*."):
        prefix, body = "*.", body[2:]
    elif body.startswith("="):
        prefix, body = "=", body[1:]
    body = body.rstrip(".")
    if not body:
        _reject(entry, "it is only a marker, with no domain after it")
    if len(body) > DOMAIN_MAX:
        _reject(entry, f"it is longer than {DOMAIN_MAX} characters")

    labels = body.split(".")
    if len(labels) < 2:
        _reject(entry, "it is a single name, not a domain such as 'example.com'")
    for label in labels:
        if not label:
            _reject(entry, "it has an empty part between two dots")
        if len(label) > LABEL_MAX:
            _reject(entry, f"the part {label!r} is longer than {LABEL_MAX} characters")
        for char in label:
            if not (char.isalnum() or char in LABEL_EXTRA_CHARS):
                _reject(entry, f"the character {char!r} cannot appear in a domain")
    return prefix + body


def _clean_domains(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        entry = (raw or "").strip()
        if not entry:
            continue
        canonical = _canonical_domain(entry)
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(canonical)
    if len(out) > MAX_LIST_ENTRIES:
        raise ValueError(f"keep the list to {MAX_LIST_ENTRIES} domains or fewer; this one has {len(out)}")
    return out


class CatalogService(BaseModel):
    key: str
    label: str = ""
    geosite: str | None = None
    domains: int
    icon: str = ""


class CatalogGroupOut(BaseModel):
    key: str
    geosite: str | None
    domains: int
    services: list[CatalogService]


class CatalogListEntry(BaseModel):
    key: str
    label: str
    domains: int


class CatalogListGroup(BaseModel):
    key: str
    domains: int
    entries: list[CatalogListEntry]


class CatalogResponse(BaseModel):
    groups: list[CatalogGroupOut]
    protection: list[CatalogListEntry] = Field(default_factory=list)
    lists: list[CatalogListGroup] = Field(default_factory=list)


class ProfilePayload(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    categories: list[str] = Field(default_factory=list)
    allow_list: list[str] = Field(default_factory=list)
    block_list: list[str] = Field(default_factory=list)
    strict_mode: bool = True
    note: str | None = Field(default=None, max_length=256)

    @field_validator("categories")
    @classmethod
    def known_categories(cls, value: list[str]) -> list[str]:
        return normalise([v.strip() for v in value if (v or "").strip()])

    @field_validator("allow_list", "block_list")
    @classmethod
    def clean_lists(cls, value: list[str]) -> list[str]:
        return _clean_domains(value)


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    categories: list[str] = Field(default_factory=list)
    allow_list: list[str] = Field(default_factory=list)
    block_list: list[str] = Field(default_factory=list)
    strict_mode: bool
    note: str | None
    created_at: dt
    updated_at: dt


class AssignmentPayload(BaseModel):
    profile_id: int
    node_id: int | None = None
    inbound_tag: str = Field(default="", max_length=256)
    is_enabled: bool = True
    confirm_restart: bool = False

    @field_validator("inbound_tag")
    @classmethod
    def trim_endpoint(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def endpoint_required_without_node(self) -> Self:
        if not self.inbound_tag and self.node_id is None:
            raise ValueError("choose a node, an endpoint, or both")
        return self


class AssignmentBulkPayload(BaseModel):
    profile_id: int
    node_ids: list[int] = Field(default_factory=list)
    inbound_tags: list[str] = Field(default_factory=list)
    is_enabled: bool = True
    confirm_restart: bool = False

    @field_validator("inbound_tags")
    @classmethod
    def clean_endpoints(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))

    @model_validator(mode="after")
    def endpoints_required_without_nodes(self) -> Self:
        if not self.inbound_tags and not self.node_ids:
            raise ValueError("choose at least one node or one endpoint")
        return self


class ReloadPrompt(BaseModel):
    reason: str = "reload_required"
    message: str
    node_ids: list[int]
    inbound_tags: list[str]
    confirm_with: str = "confirm_restart"


class AssignmentOutcome(BaseModel):
    node_id: int | None
    inbound_tag: str
    created: bool
    status: str
    enforced: bool = False
    delivery: str | None = None
    detail: str | None = None
    advisories: list[str] = Field(default_factory=list)
    advisory_note: str = ""
    reload: ReloadPrompt | None = None


class AssignmentBulkResult(BaseModel):
    created: int
    updated: int
    applied: int
    failed: int
    disabled: int
    skipped: int
    outcomes: list[AssignmentOutcome]


class AssignmentResponse(BaseModel):
    id: int
    profile_id: int
    node_id: int | None
    inbound_tag: str
    is_enabled: bool
    enforced: bool
    last_checked_at: dt | None
    last_error: str | None
    delivery: str
    reaches_nodes: list[int]
    advisories: list[str] = Field(default_factory=list)
    advisory_note: str = ""


class TargetInbound(BaseModel):
    tag: str
    protocol: str
    filterable: bool
    reason: str | None = None


class TargetNode(BaseModel):
    id: int
    name: str
    status: str
    core_config_id: int
    routing_service: bool
    reason: str | None = None
    shares_core_with: list[int] = Field(default_factory=list)
    pinned_stays_here: bool = True
    would_also_affect: list[int] = Field(default_factory=list)
    scope_note: str = ""
    inbounds: list[TargetInbound]


class DestinationTest(BaseModel):
    node_id: int
    inbound_tag: str = ""
    domain: str = Field(min_length=1, max_length=DOMAIN_MAX)


class DestinationVerdict(BaseModel):
    domain: str
    outbound: str
    blocked: bool
