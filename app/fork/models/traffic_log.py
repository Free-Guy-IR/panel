from datetime import UTC, datetime as dt

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.compiles_types import SqliteCompatibleBigInteger
from app.db.models import IdMixin


class TrafficLogRecord(Base, IdMixin):
    __tablename__ = "traffic_log_records"
    __table_args__ = (
        Index("ix_traffic_log_records_bucket_start", "bucket_start"),
        Index("ix_traffic_log_records_user_last", "user_id", "last_seen"),
        Index("ix_traffic_log_records_last_id", "last_seen", "id"),
        Index("ix_traffic_log_records_node_last", "node_id", "last_seen"),
    )

    bucket_start: Mapped[dt] = mapped_column(DateTime(timezone=True))
    node_id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger)
    inbound_tag: Mapped[str] = mapped_column(String(256))
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column()
    protocol: Mapped[str] = mapped_column(String(3))
    route: Mapped[str] = mapped_column(String(128))
    first_seen: Mapped[dt] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[dt] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[int | None] = mapped_column(SqliteCompatibleBigInteger, default=None)
    user_label: Mapped[str | None] = mapped_column(String(128), default=None)
    refused: Mapped[bool] = mapped_column(default=False, server_default="0")
    hits: Mapped[int] = mapped_column(default=1, server_default="1")


class TrafficLogIdentity(Base):
    __tablename__ = "traffic_log_identities"

    user_id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str] = mapped_column(String(128))
    admin_id: Mapped[int | None] = mapped_column(SqliteCompatibleBigInteger, default=None)
    deleted: Mapped[bool] = mapped_column(default=False, server_default="0")
    updated_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default_factory=lambda: dt.now(UTC))


class TrafficLogState(Base):
    __tablename__ = "traffic_log_state"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False, default=1)
    enabled: Mapped[bool] = mapped_column(default=True, server_default="1")
    retention_hours: Mapped[int] = mapped_column(default=48, server_default="48")
    updated_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default_factory=lambda: dt.now(UTC))
