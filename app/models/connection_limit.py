from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConnectionStateResponse(BaseModel):
    user_id: int
    username: str | None = None
    devices: int = 0
    address_sources: int = 0
    hwid_count: int = 0
    node_count: int = 0
    app_count: int = 0
    verdict: str = "within_limit"
    limit_applied: int = 0
    streak: int = 0
    node_streak: int = 0
    checked_at: datetime | None = None
    reasons: list[dict | str] = Field(default_factory=list)
    details: dict = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)

    @field_validator("reasons", mode="before")
    @classmethod
    def normalize_reasons(cls, value):
        return value or []

    @field_validator("details", mode="before")
    @classmethod
    def normalize_details(cls, value):
        return value or {}


class ConnectionStatesResponse(BaseModel):
    states: list[ConnectionStateResponse]
    total: int
    device_limit: int = 0
    enabled: bool = False
    monitor_only: bool = True


class UserConnectionLimitPayload(BaseModel):
    ip_limit: int | None = Field(default=None, ge=1, le=100)
    exempt: bool = False
    note: str | None = Field(default=None, max_length=256)


class UserConnectionLimitResponse(UserConnectionLimitPayload):
    user_id: int
    username: str | None = None

    model_config = ConfigDict(from_attributes=True)


class UserConnectionLimitsResponse(BaseModel):
    overrides: list[UserConnectionLimitResponse]
    total: int
    default_device_limit: int = 0


class ResolvedAddress(BaseModel):
    address: str
    provider: str | None = None
    country: str | None = None


class ResolvedAddressesResponse(BaseModel):
    addresses: list[ResolvedAddress]
    enabled: bool = False


class ConnectionViolationResponse(BaseModel):
    id: int
    user_id: int
    username: str | None = None
    created_at: datetime
    devices: int = 0
    limit_applied: int = 0
    observed_addresses: list[str] = Field(default_factory=list)
    reasons: list[dict | str] = Field(default_factory=list)
    step_applied: int = 0
    disable_minutes: int = 0
    restore_at: datetime | None = None
    restored_at: datetime | None = None
    active: bool = False
    model_config = ConfigDict(from_attributes=True)


class ConnectionViolationsResponse(BaseModel):
    violations: list[ConnectionViolationResponse]
    total: int
    enforcement_enabled: bool = False
    steps: list[int] = Field(default_factory=list)
    window_hours: int = 72
