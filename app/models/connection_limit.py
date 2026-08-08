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
