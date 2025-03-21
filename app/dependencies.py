from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from fastapi import Depends, HTTPException

from app.db import Session, crud, get_db
from app.models.admin import Admin
from app.models.user import UserResponse, UserStatus


def validate_dates(
    start: Optional[Union[str, datetime]], end: Optional[Union[str, datetime]]
) -> tuple[datetime, datetime]:
    """Validate if start and end dates are correct and if end is after start."""
    try:
        if start:
            start_date = (
                start if isinstance(start, datetime) else datetime.fromisoformat(start).astimezone(timezone.utc)
            )
        else:
            start_date = datetime.now(timezone.utc) - timedelta(days=30)
        if end:
            end_date = end if isinstance(end, datetime) else datetime.fromisoformat(end).astimezone(timezone.utc)
            if start_date and end_date < start_date:
                raise HTTPException(status_code=400, detail="Start date must be before end date")
        else:
            end_date = datetime.now(timezone.utc)

        return start_date, end_date
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date range or format")


def get_validated_user(
    username: str, admin: Admin = Depends(Admin.get_current), db: Session = Depends(get_db)
) -> UserResponse:
    dbuser = crud.get_user(db, username)
    if not dbuser:
        raise HTTPException(status_code=404, detail="User not found")

    if not (admin.is_sudo or (dbuser.admin and dbuser.admin.username == admin.username)):
        raise HTTPException(status_code=403, detail="You're not allowed")

    return dbuser


def get_expired_users_list(db: Session, admin: Admin, expired_after: datetime = None, expired_before: datetime = None):
    dbadmin = crud.get_admin(db, admin.username)
    dbusers = crud.get_users(
        db=db, status=[UserStatus.expired, UserStatus.limited], admin=dbadmin if not admin.is_sudo else None
    )

    return [u for u in dbusers if u.expire and expired_after <= u.expire <= expired_before]
