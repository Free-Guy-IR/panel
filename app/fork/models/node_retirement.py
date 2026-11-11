from datetime import datetime as dt

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.compiles_types import SqliteCompatibleBigInteger


class NodeRetirement(Base):
    __tablename__ = "node_retirements"
    __table_args__ = (Index("ix_node_retirements_next_attempt_at", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, init=False, autoincrement=True)
    node_id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger)
    name: Mapped[str] = mapped_column(String(256))
    connection_type: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(256))
    port: Mapped[int] = mapped_column(Integer)
    api_port: Mapped[int] = mapped_column(Integer)
    server_ca: Mapped[str] = mapped_column(String(2048))
    api_key: Mapped[str | None] = mapped_column(String(36))
    proxy_url: Mapped[str | None] = mapped_column(String(256))
    default_timeout: Mapped[int] = mapped_column(Integer)
    internal_timeout: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[dt] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[dt] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1024), default=None)
