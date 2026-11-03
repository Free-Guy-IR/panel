from sqlalchemy import event
from sqlalchemy.orm.attributes import set_committed_value

from app.db.models import APIKey


def _read_legacy_null_permissions_as_empty(target: APIKey, *_args) -> None:
    if "permissions" in target.__dict__ and target.__dict__["permissions"] is None:
        set_committed_value(target, "permissions", {})


def register() -> None:
    for identifier in ("load", "refresh"):
        if not event.contains(APIKey, identifier, _read_legacy_null_permissions_as_empty):
            event.listen(APIKey, identifier, _read_legacy_null_permissions_as_empty)
