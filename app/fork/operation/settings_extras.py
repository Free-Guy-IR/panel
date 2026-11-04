from collections import Counter
from typing import Any

from app.models.settings import Application, SettingsSchema, check_import_url

_IMPORT_URL_MAX_LENGTH = Application.model_fields["import_url"].metadata[0].max_length


def _blanked_on_load(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _IMPORT_URL_MAX_LENGTH:
        return True
    try:
        check_import_url(value)
    except ValueError:
        return True
    return False


def _platform_name(platform: Any) -> str:
    return str(getattr(platform, "value", platform))


def _stored_key(application: dict) -> tuple[str, str]:
    return str(application.get("name")), _platform_name(application.get("platform"))


def _legacy_import_urls(stored_subscription: Any) -> dict[tuple[str, str], str]:
    if not isinstance(stored_subscription, dict):
        return {}
    stored_applications = [app for app in stored_subscription.get("applications") or [] if isinstance(app, dict)]
    occurrences = Counter(_stored_key(app) for app in stored_applications)
    return {
        _stored_key(app): app["import_url"]
        for app in stored_applications
        if occurrences[_stored_key(app)] == 1 and _blanked_on_load(app.get("import_url"))
    }


def keep_legacy_import_urls(modify: SettingsSchema, stored_subscription: Any) -> None:
    if modify.subscription is None:
        return
    legacy = _legacy_import_urls(stored_subscription)
    if not legacy:
        return
    incoming = modify.subscription.applications
    occurrences = Counter((app.name, _platform_name(app.platform)) for app in incoming)
    applications = []
    for app in incoming:
        key = (app.name, _platform_name(app.platform))
        if not app.import_url and occurrences[key] == 1 and key in legacy:
            app = app.model_copy(update={"import_url": legacy[key]})
        applications.append(app)
    modify.subscription = modify.subscription.model_copy(update={"applications": applications})
