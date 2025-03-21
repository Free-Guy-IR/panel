from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app.db import Session, get_db
from app.db.crud import get_admin as get_admin_by_username
from app.models.admin import Admin, AdminValidationResult, AdminInDB
from app.utils.jwt import get_admin_payload
from config import SUDOERS


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/admin/token")


async def get_admin(db: Session, token: str) -> Admin | None:
    payload = get_admin_payload(token)
    if not payload:
        return

    db_admin = get_admin_by_username(db, payload["username"])
    if db_admin:
        if db_admin.password_reset_at:
            if not payload.get("created_at"):
                return
            if db_admin.password_reset_at > payload.get("created_at"):
                return

        return Admin.model_validate(db_admin)

    elif payload["username"] in SUDOERS and payload["is_sudo"] is True:
        return Admin(username=payload["username"], is_sudo=True)


async def get_current(db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)):
    admin: Admin | None = await get_admin(db, token)
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if admin.is_disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="your account has been disabled",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return admin


async def check_sudo_admin(db: Session = Depends(get_db), token: str = Depends(oauth2_scheme)):
    admin: Admin | None = await get_admin(db, token)
    if not admin:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if admin.is_disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="your account has been disabled",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not admin.is_sudo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You're not allowed")
    return admin


async def validate_admin(db: Session, username: str, password: str) -> AdminValidationResult | None:
    """Validate admin credentials with environment variables or database."""

    db_admin = get_admin_by_username(db, username)
    if db_admin and AdminInDB.model_validate(db_admin).verify_password(password):
        return AdminValidationResult(
            username=db_admin.username, is_sudo=db_admin.is_sudo, is_disabled=db_admin.is_disabled
        )

    if not db_admin and SUDOERS.get(username) == password:
        return AdminValidationResult(username=username, is_sudo=True, is_disabled=False)
