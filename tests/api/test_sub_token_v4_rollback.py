import importlib.util
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update

from app.db.models import User
from tests.api import GetTestDB, TestSession, client
from tests.api.helpers import auth_headers, create_user, delete_user, unique_name

ROOT = Path(__file__).resolve().parents[2]
RELEASE_COMMIT = "b3044efa2"


@pytest.fixture
def release(tmp_path, monkeypatch):
    source = subprocess.run(
        ["git", "show", f"{RELEASE_COMMIT}:app/utils/jwt.py"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    if source.returncode:
        pytest.skip(f"release commit {RELEASE_COMMIT} is not in this checkout")
    path = tmp_path / "release_jwt.py"
    path.write_text(source.stdout)
    spec = importlib.util.spec_from_file_location("release_jwt_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "GetDB", GetTestDB)
    return module


def _status(token: str) -> int:
    return client.get(f"/sub/{token}/info").status_code


def _token(subscription_url: str) -> str:
    return subscription_url.rstrip("/").rsplit("/", 1)[-1]


async def _release_revoke(release, user_id: int):
    revoked_at = release.next_revocation_instant(None) - timedelta(minutes=10)
    async with TestSession() as db:
        await db.execute(update(User).where(User.id == user_id).values(sub_revoked_at=revoked_at))
        await db.commit()


@pytest.mark.asyncio
async def test_release_links_survive_the_rollback_and_die_on_its_first_revoke(access_token, release):
    user = create_user(access_token, payload={"username": unique_name("v4_rollback")})
    try:
        never_revoked = await release.create_subscription_token(user["id"])
        assert _status(never_revoked) == 200

        await _release_revoke(release, user["id"])
        reminted = await release.create_subscription_token(user["id"])
        assert _status(never_revoked) == 404
        assert _status(reminted) == 200

        response = client.post(f"/api/user/{user['username']}/revoke_sub", headers=auth_headers(access_token))
        assert response.status_code == 200, response.text
        assert _status(reminted) == 404
        assert _status(never_revoked) == 404
        assert _status(_token(response.json()["subscription_url"])) == 200

        minted_after_the_rollback_revoke = await release.create_subscription_token(user["id"])
        assert _status(minted_after_the_rollback_revoke) == 200
    finally:
        delete_user(access_token, user["username"])
