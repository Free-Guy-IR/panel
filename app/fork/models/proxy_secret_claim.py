from datetime import datetime as dt

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.compiles_types import SqliteCompatibleBigInteger


class ProxySecretClaim(Base):
    __tablename__ = "proxy_secret_claims"
    __table_args__ = (Index("ix_proxy_secret_claims_user_id", "user_id"),)

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        SqliteCompatibleBigInteger, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, default=None
    )
    claimed_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default=None)
    retired_at: Mapped[dt | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
