from datetime import UTC, datetime

import pytest
from sqlalchemy import update

from app.db.models import User
from app.utils import jwt as jwt_utils
from tests.api import TestSession, client
from tests.api.helpers import create_user, delete_user, unique_name

MINTED_AT = 1_900_000_000.2
REVOKED_AT = 1_900_000_000.6


@pytest.mark.asyncio
async def test_a_token_minted_before_the_revoke_is_rejected(access_token, monkeypatch):
    user = create_user(access_token, payload={"username": unique_name("same_second")})
    try:
        monkeypatch.setattr(jwt_utils.time, "time", lambda: MINTED_AT)
        token = await jwt_utils.create_subscription_token(user["id"])
        monkeypatch.undo()
        async with TestSession() as db:
            await db.execute(
                update(User)
                .where(User.id == user["id"])
                .values(
                    created_at=datetime.fromtimestamp(MINTED_AT - 60, tz=UTC),
                    sub_revoked_at=datetime.fromtimestamp(REVOKED_AT, tz=UTC),
                )
            )
            await db.commit()
        response = client.get(f"/sub/{token}/info")
        assert response.status_code == 404, "a token minted 0.4 s before the revoke still opens the subscription"
    finally:
        delete_user(access_token, user["username"])
