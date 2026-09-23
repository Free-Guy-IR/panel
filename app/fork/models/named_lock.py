from datetime import datetime as dt

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class NamedLock(Base):
    __tablename__ = "named_locks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    acquired_at: Mapped[dt | None] = mapped_column(DateTime(timezone=True), default=None)
