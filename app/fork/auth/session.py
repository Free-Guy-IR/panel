from fastapi import Depends, HTTPException, status

from app.models.admin import AdminDetails
from app.operation.permissions import PermissionDenied, enforce_permission
from app.routers.authentication import get_current_session


def require_session_permission(resource: str, action: str):
    async def _check(admin: AdminDetails = Depends(get_current_session)):
        try:
            enforce_permission(admin, resource, action)
        except PermissionDenied as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
        return admin

    return _check
