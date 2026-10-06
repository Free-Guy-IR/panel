from datetime import datetime as dt

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.compiles_types import SqliteCompatibleBigInteger


class PendingNodeRemoval(Base):
    __tablename__ = "pending_node_removals"

    user_id: Mapped[int] = mapped_column(SqliteCompatibleBigInteger, primary_key=True, autoincrement=False)
    token: Mapped[str] = mapped_column(String(32))
    requested_at: Mapped[dt] = mapped_column(DateTime(timezone=True), index=True)
