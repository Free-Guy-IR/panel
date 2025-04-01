from enum import Enum
from datetime import datetime as dt

from pydantic import BaseModel, field_validator

from .validators import NumericValidatorMixin


class Period(str, Enum):
    minute = "minute"
    hour = "hour"
    day = "day"
    month = "month"


class UsageStats(BaseModel):
    downlink: int
    uplink: int
    period: Period
    period_start: dt

    @field_validator("downlink", "uplink", mode="before")
    def cast_to_int(cls, v):
        return NumericValidatorMixin.cast_to_int(v)
