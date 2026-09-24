from __future__ import annotations

from sqlalchemy.exc import DBAPIError

from app.fork.proxy_secrets.locks import NamedLockMissing, driver_code
from app.fork.proxy_secrets.uniqueness import ProxySecretUniquenessError

SAFE_MESSAGE_ERRORS = (NamedLockMissing, ProxySecretUniquenessError)


def _driver_codes(exc: DBAPIError) -> list[str]:
    code = driver_code(exc)
    candidates = (
        code if isinstance(code, int) else None,
        getattr(exc.orig, "sqlstate", None),
        getattr(exc.orig, "pgcode", None),
        getattr(exc.orig, "sqlite_errorname", None),
    )
    return [str(value) for value in candidates if value]


def describe_failure(exc: BaseException) -> str:
    if isinstance(exc, SAFE_MESSAGE_ERRORS):
        return f"{type(exc).__name__}: {exc}"
    if not isinstance(exc, DBAPIError):
        return type(exc).__name__
    label = f"{type(exc).__name__} from {type(exc.orig).__name__}"
    codes = _driver_codes(exc)
    return f"{label} ({', '.join(codes)})" if codes else label
