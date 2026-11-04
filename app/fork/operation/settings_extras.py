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


def _stored_applications(stored_subscription: Any) -> list[dict]:
    if not isinstance(stored_subscription, dict):
        return []
    return [app for app in stored_subscription.get("applications") or [] if isinstance(app, dict)]


def _legacy_import_urls(stored_applications: list[dict]) -> dict[tuple[str, str], str]:
    occurrences = Counter(_stored_key(app) for app in stored_applications)
    return {
        _stored_key(app): app["import_url"]
        for app in stored_applications
        if occurrences[_stored_key(app)] == 1 and _blanked_on_load(app.get("import_url"))
    }


def keep_legacy_import_urls(modify: SettingsSchema, stored_subscription: Any) -> None:
    if modify.subscription is None:
        return
    stored_applications = _stored_applications(stored_subscription)
    legacy = _legacy_import_urls(stored_applications)
    if not legacy:
        return
    stored_keys = {_stored_key(app) for app in stored_applications}
    incoming = modify.subscription.applications
    occurrences = Counter((app.name, _platform_name(app.platform)) for app in incoming)
    platforms_with_new_names = {platform for name, platform in occurrences if (name, platform) not in stored_keys}
    applications = []
    for app in incoming:
        key = (app.name, _platform_name(app.platform))
        if not app.import_url and occurrences[key] == 1 and key in legacy and key[1] not in platforms_with_new_names:
            app = app.model_copy(update={"import_url": legacy[key]})
        applications.append(app)
    modify.subscription = modify.subscription.model_copy(update={"applications": applications})
