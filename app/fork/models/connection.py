from datetime import UTC, datetime as dt

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models import IdMixin, PostgresJSONB, fk_id_column


class UserConnectionLimit(Base, IdMixin):
    __tablename__ = "user_connection_limits"
    user_id: Mapped[int] = fk_id_column("users.id", ondelete="CASCADE", unique=True)
    ip_limit: Mapped[int | None] = mapped_column(default=None)
    exempt: Mapped[bool] = mapped_column(default=False, server_default="0")
    note: Mapped[str | None] = mapped_column(String(256), default=None)


class ConnectionRestriction(Base, IdMixin):
    __tablename__ = "connection_restrictions"
    __table_args__ = (
        Index("ix_connection_restrictions_active", "active"),
        Index("ix_connection_restrictions_user_active", "user_id", "active"),
    )
    user_id: Mapped[int] = fk_id_column("users.id", ondelete="CASCADE")
    created_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default_factory=lambda: dt.now(UTC))
    ip_count: Mapped[int] = mapped_column(default=0)
    ip_limit: Mapped[int] = mapped_column(default=0)
    observed_ips: Mapped[list | None] = mapped_column(PostgresJSONB, default=None)
    method: Mapped[str] = mapped_column(String(16), default="disable")
    step_applied: Mapped[int] = mapped_column(default=0, server_default="0")
    disable_minutes: Mapped[int] = mapped_column(default=0, server_default="0")
    previous_status: Mapped[str | None] = mapped_column(String(16), default=None)
    previous_group_ids: Mapped[list | None] = mapped_column(PostgresJSONB, default=None)
    reasons: Mapped[list | None] = mapped_column(PostgresJSONB, default=None)
    restore_at: Mapped[dt | None] = mapped_column(DateTime(timezone=True), default=None)
    restored_at: Mapped[dt | None] = mapped_column(DateTime(timezone=True), default=None)
    active: Mapped[bool] = mapped_column(default=True, server_default="1")


class UserConnectionState(Base, IdMixin):
    __tablename__ = "user_connection_states"
    __table_args__ = (
        Index("ix_user_connection_states_verdict", "verdict"),
        Index("ix_user_connection_states_checked_at", "checked_at"),
    )
    user_id: Mapped[int] = fk_id_column("users.id", ondelete="CASCADE", unique=True)
    checked_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default_factory=lambda: dt.now(UTC))
    devices: Mapped[int] = mapped_column(default=0)
    address_sources: Mapped[int] = mapped_column(default=0)
    hwid_count: Mapped[int] = mapped_column(default=0)
    node_count: Mapped[int] = mapped_column(default=0)
    app_count: Mapped[int] = mapped_column(default=0)
    verdict: Mapped[str] = mapped_column(String(16), default="within_limit")
    limit_applied: Mapped[int] = mapped_column(default=0)
    streak: Mapped[int] = mapped_column(default=0)
    node_streak: Mapped[int] = mapped_column(default=0, server_default="0")
    at_once_streak: Mapped[int] = mapped_column(default=0, server_default="0")
    reasons: Mapped[list | None] = mapped_column(PostgresJSONB, default=None)
    details: Mapped[dict | None] = mapped_column(PostgresJSONB, default=None)
