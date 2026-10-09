import hmac
import time
from base64 import b64decode, b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import ceil

import jwt
from aiocache import cached
from sqlalchemy import select

from app.db import GetDB
from app.db.crud.general import get_jwt_secret_key
from app.db.models import User
from config import jwt_settings

_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_READ_FROM_DATABASE = object()


@cached(ttl=60)
async def get_secret_key():
    async with GetDB() as db:
        key = await get_jwt_secret_key(db=db)
        return key


async def create_admin_token(admin_id: int | None, username: str) -> str:
    data = {"sub": username, "access": "admin", "iat": datetime.now(UTC)}
    if admin_id is not None:
        data["aid"] = int(admin_id)
    if jwt_settings.access_token_expire_minutes > 0:
        expire = datetime.now(UTC) + timedelta(minutes=jwt_settings.access_token_expire_minutes)
        data["exp"] = expire
    encoded_jwt = jwt.encode(data, await get_secret_key(), algorithm="HS256")
    return encoded_jwt


async def get_admin_payload(token: str) -> dict | None:
    try:
        required = ["iat", "exp"] if jwt_settings.access_token_expire_minutes > 0 else ["iat"]
        payload = jwt.decode(
            token, await get_secret_key(), algorithms=["HS256"], leeway=5, options={"require": required}
        )
        username: str = payload.get("sub")
        access: str = payload.get("access")
        admin_id = payload.get("aid")
        if admin_id is not None:
            try:
                admin_id = int(admin_id)
            except (TypeError, ValueError):
                return
        if not username or access not in ("admin", "sudo"):
            return
        try:
            created_at = datetime.fromtimestamp(payload["iat"], tz=UTC)
        except (OverflowError, OSError, ValueError):
            return

        return {
            "admin_id": admin_id,
            "username": username,
            "created_at": created_at,
        }
    except jwt.exceptions.PyJWTError:
        return


def revocation_epoch(sub_revoked_at: datetime | None) -> int:
    if sub_revoked_at is None:
        return 0
    if sub_revoked_at.tzinfo is None:
        sub_revoked_at = sub_revoked_at.replace(tzinfo=UTC)
    microseconds = (sub_revoked_at - _UNIX_EPOCH) // timedelta(microseconds=1)
    return (microseconds + 500_000) // 1_000_000


def next_revocation_instant(previous: datetime | None) -> datetime:
    now = datetime.now(UTC).replace(microsecond=0)
    if previous is None:
        return now
    after_previous = datetime.fromtimestamp(revocation_epoch(previous) + 1, tz=UTC)
    return max(now, after_previous)


async def _stored_sub_revoked_at(user_id: int) -> datetime | None:
    async with GetDB() as db:
        result = await db.execute(select(User.sub_revoked_at).where(User.id == user_id))
        return result.scalar_one_or_none()


async def create_subscription_token(user_id: int, sub_revoked_at: object = _READ_FROM_DATABASE) -> str:
    if sub_revoked_at is _READ_FROM_DATABASE:
        sub_revoked_at = await _stored_sub_revoked_at(user_id)
    data = f"v4,{user_id},{ceil(time.time())},{revocation_epoch(sub_revoked_at)}"
    data_b64_str = b64encode(data.encode("utf-8"), altchars=b"-_").decode("utf-8").rstrip("=")
    secret = await get_secret_key()
    # HMAC-SHA256 over the payload, url-safe base64, no truncation.
    # The "." separator never occurs in the legacy format (altchars=-_ payload + hex/_-  signature),
    # so its presence is what marks a token as the new HMAC format.
    signature = (
        b64encode(
            hmac.new(secret.encode("utf-8"), data_b64_str.encode("utf-8"), sha256).digest(),
            altchars=b"-_",
        )
        .decode("utf-8")
        .rstrip("=")
    )
    return data_b64_str + "." + signature


def _parse_subscription_data(data_str: str) -> dict | None:
    """Parse the decoded subscription payload string into a result dict."""
    parts = data_str.split(",")
    if len(parts) == 4 and parts[0] == "v4":
        _, u_user_id_str, u_created_at_str, u_epoch_str = parts
        try:
            u_user_id = int(u_user_id_str)
            u_created_at = datetime.fromtimestamp(int(u_created_at_str), tz=UTC)
            u_epoch = int(u_epoch_str)
        except (OverflowError, OSError, ValueError):
            return
        return {
            "user_id": u_user_id,
            "created_at": u_created_at,
            "revocation_epoch": u_epoch,
        }

    if len(parts) == 3 and parts[0] in ("v2", "v3"):
        _, u_user_id_str, u_created_at_str = parts
        try:
            u_user_id = int(u_user_id_str)
            u_created_at = datetime.fromtimestamp(int(u_created_at_str), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return
        return {
            "user_id": u_user_id,
            "created_at": u_created_at,
        }

    if len(parts) == 2:
        u_username, u_created_at_str = parts
        try:
            u_created_at = datetime.fromtimestamp(int(u_created_at_str), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return
        return {
            "username": u_username,
            "created_at": u_created_at,
        }
    return


def _decode_b64_token(data_b64_str: str) -> str | None:
    try:
        decoded = b64decode(
            (data_b64_str.encode("utf-8") + b"=" * (-len(data_b64_str.encode("utf-8")) % 4)),
            altchars=b"-_",
            validate=True,
        )
        return decoded.decode("utf-8")
    except Exception:
        return


async def get_subscription_payload(token: str) -> dict | None:
    try:
        if len(token) < 15:
            return

        if token.startswith("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."):
            payload = jwt.decode(token, await get_secret_key(), algorithms=["HS256"])
            if payload.get("access") == "subscription":
                username = payload.get("sub")
                if not username:
                    return
                try:
                    created_at = datetime.fromtimestamp(payload["iat"], tz=UTC)
                except (OverflowError, OSError, ValueError):
                    return
                return {
                    "username": username,
                    "created_at": created_at,
                }
            else:
                return

        # New HMAC format: "<b64payload>.<b64signature>". The "." never appears in the
        # legacy format, so it unambiguously identifies a new-style token.
        if "." in token:
            data_b64_str, _, u_signature = token.rpartition(".")
            secret = await get_secret_key()
            expected = (
                b64encode(
                    hmac.new(secret.encode("utf-8"), data_b64_str.encode("utf-8"), sha256).digest(),
                    altchars=b"-_",
                )
                .decode("utf-8")
                .rstrip("=")
            )
            if not hmac.compare_digest(u_signature.encode("utf-8"), expected.encode("utf-8")):
                return
            data_str = _decode_b64_token(data_b64_str)
            if data_str is None:
                return
            return _parse_subscription_data(data_str)

        u_token = token[:-10]
        u_signature = token[-10:]
        u_token_dec_str = _decode_b64_token(u_token)
        if u_token_dec_str is None:
            return
        secret = await get_secret_key()
        digest = sha256((u_token + secret).encode("utf-8")).digest()
        b64_resign = b64encode(digest, altchars=b"-_").decode("utf-8")[:10]
        hex_resign = digest.hex()[:10]
        if not (
            hmac.compare_digest(u_signature.encode("utf-8"), b64_resign.encode("utf-8"))
            or hmac.compare_digest(u_signature.encode("utf-8"), hex_resign.encode("utf-8"))
        ):
            return
        return _parse_subscription_data(u_token_dec_str)
    except jwt.exceptions.PyJWTError:
        return
