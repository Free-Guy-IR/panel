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
    # Consecutive cycles this verdict has held. One cycle is noise; the UI
    # uses this to decide how much to make of a reading.
    streak: int = 0
    checked_at: datetime | None = None
    reasons: list[str] = Field(default_factory=list)
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
