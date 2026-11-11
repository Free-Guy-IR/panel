from fastapi import APIRouter, Depends

from app.db import AsyncSession, get_db
from app.fork.auth.settings_access import SettingsCredentials, settings_credentials
from app.models.settings import General, SettingsSchema, SettingsSchemaInput
from app.operation import OperatorType
from app.operation.settings import SettingsOperation
from app.utils import responses

from .authentication import require_permission

settings_operator = SettingsOperation(operator_type=OperatorType.API)
router = APIRouter(tags=["Settings"], prefix="/api/settings", responses={401: responses._401, 403: responses._403})


@router.get("", response_model=SettingsSchema)
async def get_settings(
    db: AsyncSession = Depends(get_db),
    _=Depends(require_permission("settings", "read")),
    credentials: SettingsCredentials = Depends(settings_credentials),
):
    return credentials.redact(await settings_operator.get_settings(db))


@router.get("/general", response_model=General)
async def get_general_settings(
    db: AsyncSession = Depends(get_db), _=Depends(require_permission("settings", "read_general"))
):
    return await settings_operator.get_general_settings(db)


@router.put("", response_model=SettingsSchema)
async def modify_settings(
    modify: SettingsSchemaInput,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_permission("settings", "update")),
    credentials: SettingsCredentials = Depends(settings_credentials),
):
    credentials.authorize_changes(modify)
    return credentials.redact(await settings_operator.modify_settings(db, modify))
