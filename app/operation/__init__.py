from enum import IntEnum
from datetime import timezone, timedelta, datetime as dt

from fastapi import HTTPException

from app.db import Session
from app.db.models import ProxyHost, User, Admin as DBAdmin
from app.db.crud import get_host_by_id, get_user, get_admin
from app.utils.jwt import get_subscription_payload
from app.models.admin import Admin


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
        """Raise an error based on the operator type."""
        if code <= 0:
            code = 408
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

    async def get_validated_host(self, db: Session, host_id: int) -> ProxyHost:
        db_host = get_host_by_id(db, host_id)
        if db_host is None:
            self.raise_error(message="Host not found", code=404)
        return db_host

    async def get_validated_sub(self, db: Session, token: str) -> User:
        sub = get_subscription_payload(token)
        if not sub:
            self.raise_error(message="Not Found", code=404)

        db_user = get_user(db, sub["username"])
        if not db_user or db_user.created_at > sub["created_at"]:
            self.raise_error(message="Not Found", code=404)

        if db_user.sub_revoked_at and db_user.sub_revoked_at > sub["created_at"]:
            self.raise_error(message="Not Found", code=404)

        return db_user

    async def get_validated_user(self, db: Session, username: str, admin: Admin) -> User:
        db_user = get_user(db, username)
        if not db_user:
            self.raise_error(message="User not found", code=404)

        if not (admin.is_sudo or (db_user.admin and db_user.admin.username == admin.username)):
            self.raise_error(message="You're not allowed", code=403)

        return db_user

    async def get_validated_admin(self, db: Session, username: str) -> DBAdmin:
        db_admin = get_admin(db, username)
        if not db_admin:
            self.raise_error(message="Admin not found", code=404)
        return db_admin
