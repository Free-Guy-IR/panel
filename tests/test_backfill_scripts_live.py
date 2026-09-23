import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.crud.user import revoke_user_sub
from app.db.models import Admin, AdminRole, Base, User
from app.models.proxy import ProxyTable

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a3b.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(engine):
    return async_sessionmaker(engine, class_=AsyncSession, autoflush=False, expire_on_commit=False)


async def _seed(sessions, protocol):
    async with sessions() as db:
        role = AdminRole(name="a3b-owner", is_owner=True)
        db.add(role)
        await db.flush()
        admin = Admin(username="a3b-owner", hashed_password="x", role_id=role.id)
        db.add(admin)
        await db.flush()
        settings = ProxyTable().dict()
        settings.pop(protocol)
        user = User(username="a3b-legacy", proxy_settings=settings, admin_id=admin.id)
        db.add(user)
        await db.commit()
        return user.id


def _bind(module, sessions):
    class _TestGetDB:
        def __init__(self):
            self.db = sessions()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *exc):
            await self.db.close()

    module.GetDB = _TestGetDB


@pytest.mark.parametrize(
    ("script", "protocol"),
    [("backfill_hysteria2_passwords", "hysteria2"), ("backfill_openvpn_passwords", "openvpn")],
)
@pytest.mark.asyncio
async def test_a_second_run_changes_nothing(sessions, script, protocol):
    module = _load(script)
    _bind(module, sessions)
    user_id = await _seed(sessions, protocol)

    await module.main()
    async with sessions() as db:
        first = await db.scalar(select(User.proxy_settings).where(User.id == user_id))
    await module.main()
    async with sessions() as db:
        second = await db.scalar(select(User.proxy_settings).where(User.id == user_id))

    assert first[protocol]["password"]
    assert first == second


@pytest.mark.parametrize(
    ("script", "protocol"),
    [("backfill_hysteria2_passwords", "hysteria2"), ("backfill_openvpn_passwords", "openvpn")],
)
@pytest.mark.asyncio
async def test_a_run_on_a_live_panel_keeps_a_revoke_committed_while_it_ran(sessions, script, protocol):
    module = _load(script)
    _bind(module, sessions)
    user_id = await _seed(sessions, protocol)
    real_password = module.random_password
    state = SimpleNamespace(revoked=None, hooked=False)

    def password_after_a_concurrent_revoke():
        return real_password()

    async def revoke():
        async with sessions() as db:
            db_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
            revoked = ProxyTable()
            await revoke_user_sub(db, db_user, proxy_settings=revoked.dict())
            state.revoked = str(revoked.vless.id)

    real_main = module.main

    async def main_with_interleave():
        original_execute = AsyncSession.execute

        async def execute_then_revoke(self, *args, **kwargs):
            result = await original_execute(self, *args, **kwargs)
            if not state.hooked:
                state.hooked = True
                users = result.scalars().all()
                await revoke()

                class _Replay:
                    def scalars(self_inner):
                        return SimpleNamespace(all=lambda: users)

                return _Replay()
            return result

        AsyncSession.execute = execute_then_revoke
        try:
            await real_main()
        finally:
            AsyncSession.execute = original_execute

    module.random_password = password_after_a_concurrent_revoke
    await main_with_interleave()

    async with sessions() as db:
        stored = await db.scalar(select(User.proxy_settings).where(User.id == user_id))
    assert stored["vless"]["id"] == state.revoked, "the backfill wrote back the credential that was revoked while it ran"
