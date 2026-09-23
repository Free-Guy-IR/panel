from datetime import datetime as dt

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.compiles_types import SqliteCompatibleBigInteger


class PendingUserResync(Base):
    __tablename__ = "pending_user_resyncs"

    user_id: Mapped[int] = mapped_column(
        SqliteCompatibleBigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    token: Mapped[str] = mapped_column(String(32))
    requested_at: Mapped[dt] = mapped_column(DateTime(timezone=True))
