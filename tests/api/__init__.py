import asyncio
import json
from fastapi.testclient import TestClient
from decouple import config
from sqlalchemy.pool import StaticPool, NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.exc import SQLAlchemyError
from app import app
from app.db import get_db
from app.db.base import Base
from config import SQLALCHEMY_DATABASE_URL

XRAY_JSON_TEST_FILE = "tests/api/xray_config-test.json"

TEST_FROM = config("TEST_FROM", default="local")
DATABASE_URL = "sqlite+aiosqlite:///:memory:" if TEST_FROM == "local" else SQLALCHEMY_DATABASE_URL
print(f"TEST_FROM: {TEST_FROM}")
print(f"DATABASE_URL: {DATABASE_URL}")

IS_SQLITE = DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    engine = create_async_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False, "uri": True},
        poolclass=StaticPool,
        # echo=True,
    )
else:
    engine = create_async_engine(
        DATABASE_URL,
        poolclass=NullPool,  # Important for tests
        # echo=True,  # For debugging
    )
TestSession = async_sessionmaker(autocommit=False, autoflush=False, bind=engine)

if TEST_FROM == "local":

    async def create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(create_tables())


class GetTestDB:
    def __init__(self):
        self.db = TestSession()

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc_value, traceback):
        if isinstance(exc_value, SQLAlchemyError):
            await self.db.rollback()  # rollback on exception

        await self.db.close()


async def get_test_db():
    async with GetTestDB() as db:
        yield db


app.dependency_overrides[get_db] = get_test_db


with open(XRAY_JSON_TEST_FILE, "w") as f:
    f.write(
        json.dumps(
            {
                "log": {"loglevel": "warning"},
                "routing": {"rules": [{"ip": ["geoip:private"], "outboundTag": "BLOCK", "type": "field"}]},
                "inbounds": [
                    {
                        "tag": "Shadowsocks TCP",
                        "listen": "0.0.0.0",
                        "port": 1080,
                        "protocol": "shadowsocks",
                        "settings": {"clients": [], "network": "tcp,udp"},
                    }
                ],
                "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}, {"protocol": "blackhole", "tag": "BLOCK"}],
            },
            indent=4,
        )
    )


client = TestClient(app)
