import asyncio
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, status
from sqlalchemy import Select, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.models import PasskeyChallenge
from app.routers import passkey as passkey_router
from config import auth_settings
from tests.api import DATABASE_URL, IS_SQLITE, TestSession, client


class _RacingSession(AsyncSession):
    barrier: asyncio.Barrier | None = None

    async def execute(self, statement, *args, **kwargs):
        result = await super().execute(statement, *args, **kwargs)
        if isinstance(statement, Select) and self.barrier is not None:
            try:
                await asyncio.wait_for(self.barrier.wait(), timeout=2)
            except TimeoutError, asyncio.BrokenBarrierError:
                pass
        return result


def _store_challenge(challenge: bytes, kind: str) -> None:
    async def _store() -> None:
        async with TestSession() as session:
            session.add(
                PasskeyChallenge(
                    challenge=challenge, admin_id=None, kind=kind, expires_at=datetime.now(UTC) + timedelta(minutes=2)
                )
            )
            await session.commit()

    asyncio.run(_store())


def _remaining(challenge: bytes) -> int:
    async def _count() -> int:
        async with TestSession() as session:
            rows = await session.execute(select(PasskeyChallenge).where(PasskeyChallenge.challenge == challenge))
            return len(rows.scalars().all())

    return asyncio.run(_count())


def _drop(challenge: bytes) -> None:
    async def _delete() -> None:
        async with TestSession() as session:
            await session.execute(delete(PasskeyChallenge).where(PasskeyChallenge.challenge == challenge))
            await session.commit()

    asyncio.run(_delete())


@pytest.mark.skipif(":memory:" in DATABASE_URL, reason="needs a database shared by several connections")
@pytest.mark.parametrize("contenders", [2, 4])
def test_a_challenge_is_consumed_by_exactly_one_concurrent_request(contenders):
    challenge = secrets.token_bytes(32)
    _store_challenge(challenge, "login")

    async def _scenario() -> list[str]:
        connect_args = {"timeout": 30} if IS_SQLITE else {}
        engine = create_async_engine(DATABASE_URL, poolclass=NullPool, connect_args=connect_args)
        sessions = async_sessionmaker(engine, class_=_RacingSession, expire_on_commit=False)
        barrier = asyncio.Barrier(contenders)

        async def _consume() -> str:
            async with sessions() as db:
                db.barrier = barrier
                try:
                    await passkey_router._consume_challenge(db, challenge, "login", None)
                    return "consumed"
                except HTTPException as exc:
                    await db.rollback()
                    return f"rejected:{exc.status_code}"

        try:
            return await asyncio.gather(*(_consume() for _ in range(contenders)))
        finally:
            await engine.dispose()

    try:
        outcomes = asyncio.run(_scenario())
        assert outcomes.count("consumed") == 1, outcomes
        assert outcomes.count("rejected:400") == contenders - 1, outcomes
        assert _remaining(challenge) == 0
    finally:
        _drop(challenge)


def test_a_consumed_challenge_cannot_be_replayed():
    challenge = secrets.token_bytes(32)
    _store_challenge(challenge, "login")

    async def _twice() -> list[str]:
        outcomes = []
        for _ in range(2):
            async with TestSession() as db:
                try:
                    await passkey_router._consume_challenge(db, challenge, "login", None)
                    outcomes.append("consumed")
                except HTTPException as exc:
                    outcomes.append(f"rejected:{exc.status_code}")
        return outcomes

    try:
        assert asyncio.run(_twice()) == ["consumed", "rejected:400"]
    finally:
        _drop(challenge)


def test_a_challenge_of_another_kind_is_not_consumed():
    challenge = secrets.token_bytes(32)
    _store_challenge(challenge, "register")

    async def _attempt() -> str:
        async with TestSession() as db:
            try:
                await passkey_router._consume_challenge(db, challenge, "login", None)
                return "consumed"
            except HTTPException as exc:
                return f"rejected:{exc.status_code}"

    try:
        assert asyncio.run(_attempt()) == "rejected:400"
        assert _remaining(challenge) == 1
    finally:
        _drop(challenge)


@pytest.fixture
def configured_rp_id(monkeypatch):
    monkeypatch.setattr(auth_settings, "passkey_rp_id", "example.com")
    monkeypatch.setattr(auth_settings, "passkey_origin", "")


def test_a_configured_rp_id_is_used_without_a_configured_origin(configured_rp_id):
    response = client.post("/api/admin/passkey/login/options", json={}, headers={"Origin": "https://panel.example.com"})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["rpId"] == "example.com"


def test_a_configured_rp_id_for_another_host_is_not_sent_to_the_browser(configured_rp_id):
    response = client.post("/api/admin/passkey/login/options", json={}, headers={"Origin": "https://other.test"})

    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json()["rpId"] == "other.test"
