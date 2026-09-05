from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ConnectionStateResponse(BaseModel):
    """What the panel last observed about one subscription."""

    user_id: int
    username: str | None = None
    devices: int = 0
    address_sources: int = 0
    hwid_count: int = 0
    node_count: int = 0
    app_count: int = 0
    verdict: str = "within_limit"
    limit_applied: int = 0
    # Consecutive cycles this verdict has held. One cycle is noise; the UI
    # uses this to decide how much to make of a reading.
    streak: int = 0
    # Checks in a row on more than one node, which is what makes that worth
    # reporting rather than a single refresh of an app that tries every server.
    node_streak: int = 0
    checked_at: datetime | None = None
    # Codes plus their values; the frontend renders them in the panel's language.
    # Rows written before reasons were structured hold plain sentences instead,
    # and stay that way until their user is next seen - so both shapes are read.
    reasons: list[dict | str] = Field(default_factory=list)
    details: dict = Field(default_factory=dict)

    model_config = ConfigDict(from_attributes=True)


class ConnectionStatesResponse(BaseModel):
    states: list[ConnectionStateResponse]
    total: int
    # Echoed so the UI can label a row against the limit in force when it was
    # measured, rather than whatever the limit happens to be now.
    device_limit: int = 0
    enabled: bool = False
    monitor_only: bool = True


class UserConnectionLimitPayload(BaseModel):
    """A device allowance for one user, or an exemption from checking."""

    # null means "use the default from settings".
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
    # False when provider lookup is switched off, so the UI can say why the
    # names are missing rather than showing blanks.
    enabled: bool = False


class ConnectionViolationResponse(BaseModel):
    """One time the limiter acted on a user, and what it did."""

    id: int
    user_id: int
    username: str | None = None
    created_at: datetime
    devices: int = 0
    limit_applied: int = 0
    observed_addresses: list[str] = Field(default_factory=list)
    reasons: list[dict] = Field(default_factory=list)
    # Zero is a warning that changed nothing, -1 a disable only a person lifts.
    step_applied: int = 0
    disable_minutes: int = 0
    restore_at: datetime | None = None
    restored_at: datetime | None = None
    active: bool = False
    model_config = ConfigDict(from_attributes=True)


class ConnectionViolationsResponse(BaseModel):
    violations: list[ConnectionViolationResponse]
    total: int
    # Echoed so the page can say what would happen next without asking twice.
    enforcement_enabled: bool = False
    steps: list[int] = Field(default_factory=list)
    window_hours: int = 72
