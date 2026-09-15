from datetime import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.fork.content_filter.catalog import unknown_keys

MAX_LIST_ENTRIES = 512
DOMAIN_MAX = 253


def _clean_domains(values: list[str] | None) -> list[str]:
    if not values:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = (raw or "").strip().lower().rstrip(".")
        if not value or len(value) > DOMAIN_MAX:
            continue
        if any(ch.isspace() or ch in '"\'\\/' for ch in value):
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out[:MAX_LIST_ENTRIES]


class CatalogService(BaseModel):
    key: str
    geosite: str
    domains: int


class CatalogGroupOut(BaseModel):
    key: str
    geosite: str | None
    domains: int
    services: list[CatalogService]


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
        cleaned = [v.strip() for v in value if (v or "").strip()]
        bad = unknown_keys(cleaned)
        if bad:
            raise ValueError(f"unknown categories: {', '.join(bad)}")
        return sorted(set(cleaned))

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
    node_id: int
    inbound_tag: str = Field(default="", max_length=256)
    is_enabled: bool = True


class AssignmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    profile_id: int
    node_id: int
    inbound_tag: str
    is_enabled: bool
    enforced: bool
    last_checked_at: dt | None
    last_error: str | None


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
    inbounds: list[TargetInbound]


class DestinationTest(BaseModel):
    node_id: int
    inbound_tag: str = ""
    domain: str = Field(min_length=1, max_length=DOMAIN_MAX)


class DestinationVerdict(BaseModel):
    domain: str
    outbound: str
    blocked: bool
