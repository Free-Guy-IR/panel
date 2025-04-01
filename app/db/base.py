from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.exc import SQLAlchemyError

from config import (
    SQLALCHEMY_DATABASE_URL,
    SQLALCHEMY_POOL_SIZE,
    SQLIALCHEMY_MAX_OVERFLOW,
)

IS_SQLITE = SQLALCHEMY_DATABASE_URL.startswith("sqlite")

if IS_SQLITE:
    engine = create_async_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
else:
    engine = create_async_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_size=SQLALCHEMY_POOL_SIZE,
        max_overflow=SQLIALCHEMY_MAX_OVERFLOW,
        pool_recycle=3600,
        pool_timeout=10,
    )

SessionLocal = async_sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Determine dialect once at startup based on connection URL
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    DATABASE_DIALECT = "sqlite"
elif SQLALCHEMY_DATABASE_URL.startswith("postgresql"):
    DATABASE_DIALECT = "postgresql"
elif SQLALCHEMY_DATABASE_URL.startswith("mysql"):
    DATABASE_DIALECT = "mysql"
else:
    raise ValueError("Unsupported database URL")


class Base(DeclarativeBase):
    pass


class GetDB:  # Context Manager
    def __init__(self):
        self.db = SessionLocal()

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc_value, traceback):
        if isinstance(exc_value, SQLAlchemyError):
            await self.db.rollback()  # rollback on exception

        await self.db.close()


async def get_db():  # Dependency
    async with GetDB() as db:
        yield db
