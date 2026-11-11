from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, status

from app.models.admin import AdminDetails
from app.models.settings import SettingsSchema
from app.routers.authentication import get_current

CREDENTIAL_SECTIONS = ("telegram", "notification_settings", "webhook")
OWNER_SESSION_ONLY_DETAIL = (
    "Only the owner, signed in with a session, can change the Telegram, notification and webhook settings"
)


def is_owner_session(admin: AdminDetails) -> bool:
    return admin.is_owner and admin.api_key_id is None


@dataclass(frozen=True)
class SettingsCredentials:
    reveal: bool

    def authorize_changes(self, modify: SettingsSchema) -> None:
        if self.reveal or all(getattr(modify, section) is None for section in CREDENTIAL_SECTIONS):
            return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=OWNER_SESSION_ONLY_DETAIL)

    def redact(self, settings: Any) -> Any:
        if self.reveal:
            return settings
        return SettingsSchema.model_validate(settings).model_copy(update=dict.fromkeys(CREDENTIAL_SECTIONS))


async def settings_credentials(admin: AdminDetails = Depends(get_current)) -> SettingsCredentials:
    return SettingsCredentials(reveal=is_owner_session(admin))
