from datetime import UTC, datetime as dt
from typing import Annotated, Self

from pydantic import AfterValidator, BaseModel, Field, model_validator

from app.fork.traffic_log.collector import RETENTION_MAX_HOURS, RETENTION_MIN_HOURS


def as_utc(value: dt) -> dt:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_utc_or_none(value: dt | None) -> dt | None:
    return None if value is None else as_utc(value)


UtcDatetime = Annotated[dt, AfterValidator(as_utc)]
OptionalUtcDatetime = Annotated[dt | None, AfterValidator(_as_utc_or_none)]


class LiveEvent(BaseModel):
    at: UtcDatetime
    user_id: int | None = None
    username: str | None = None
    node_id: int
    node: str | None = None
    inbound: str
    host: str
    port: int
    protocol: str
    route: str
    refused: bool


class HistoryItem(BaseModel):
    id: int
    bucket_start: UtcDatetime
    first_seen: UtcDatetime
    last_seen: UtcDatetime
    hits: int
    user_id: int | None = None
    username: str | None = None
    user_deleted: bool = False
    node_id: int
    node: str | None = None
    inbound: str
    host: str
    port: int
    protocol: str
    route: str
    refused: bool


class HistoryPage(BaseModel):
    items: list[HistoryItem] = Field(default_factory=list)
    next_cursor: str | None = None


class TopUser(BaseModel):
    user_id: int | None = None
    username: str | None = None
    hits: int


class TopDestination(BaseModel):
    host: str
    hits: int
    refused: int = 0


class Summary(BaseModel):
    connections: int = 0
    destinations: int = 0
    users: int = 0
    refused: int = 0
    top_users: list[TopUser] = Field(default_factory=list)
    top_destinations: list[TopDestination] = Field(default_factory=list)


class NodeStatus(BaseModel):
    node_id: int
    node: str | None = None
    state: str
    since: OptionalUtcDatetime = None
    lines: int = 0
    events: int = 0
    dropped: int = 0
    dropped_records: int = 0
    dropped_live: int = 0
    dropped_viewer: int = 0
    stream_full: int = 0
    restreams: int = 0
    records: int = 0
    last_event_at: OptionalUtcDatetime = None
    detail: str | None = None


class Status(BaseModel):
    enabled: bool
    available: bool
    reason: str | None = None
    retention_hours: int
    max_records: int
    ceiling_active: bool = False
    purge_incomplete: bool = False
    last_purge_at: OptionalUtcDatetime = None
    purged_expired: int = 0
    purged_over_ceiling: int = 0
    nodes: list[NodeStatus] = Field(default_factory=list)


class SettingsUpdate(BaseModel):
    enabled: bool | None = None
    retention_hours: int | None = Field(default=None, ge=RETENTION_MIN_HOURS, le=RETENTION_MAX_HOURS)

    @model_validator(mode="after")
    def _needs_a_field(self) -> Self:
        if self.enabled is None and self.retention_hours is None:
            raise ValueError("send enabled, retention_hours, or both")
        return self


class PurgeRequest(BaseModel):
    older_than_hours: int | None = Field(default=None, ge=0)
    reclaim: bool = False


class PurgeResult(BaseModel):
    removed: int
    incomplete: bool
    remaining: int
    retention_hours: int
    reclaimed: bool = False
    freed_bytes: int | None = None
