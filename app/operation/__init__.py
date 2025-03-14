from enum import IntEnum
from datetime import timezone, timedelta, datetime as dt

from fastapi import HTTPException


class OperatorType(IntEnum):
    SYSTEM = 0
    API = 1
    WEB = 2
    CLI = 3
    TELEGRAM = 4
    DISCORD = 5


class BaseOperator:
    def __init__(self, operator_type: OperatorType):
        self.operator_type = operator_type

    def raise_error(self, message: str, code: int):
        if self.operator_type in (OperatorType.API, OperatorType.WEB):
            raise HTTPException(status_code=code, detail=message)
        else:
            raise ValueError(message)

    def validate_dates(self, start: str | dt | None, end: str | dt | None) -> tuple[dt, dt]:
        """Validate if start and end dates are correct and if end is after start."""
        try:
            if start:
                start_date = start if isinstance(start, dt) else dt.fromisoformat(start).astimezone(timezone.utc)
            else:
                start_date = dt.now(timezone.utc) - timedelta(days=30)
            if end:
                end_date = end if isinstance(end, dt) else dt.fromisoformat(end).astimezone(timezone.utc)
                if start_date and end_date < start_date:
                    self.raise_error(message="Start date must be before end date", code=400)
            else:
                end_date = dt.now(timezone.utc)

            return start_date, end_date
        except ValueError:
            self.raise_error(message="Invalid date range or format", code=400)
