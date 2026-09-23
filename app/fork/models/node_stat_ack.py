from datetime import UTC, datetime as dt

from sqlalchemy import BigInteger, DateTime, Index, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.compiles_types import SqliteCompatibleBigInteger


class NodeStatAck(Base):
    __tablename__ = "node_stat_acks"
    __table_args__ = (Index("ix_node_stat_acks_node_id_stat_type", "node_id", "stat_type"),)

    batch_epoch: Mapped[str] = mapped_column(String(64), primary_key=True)
    stat_type: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    node_id: Mapped[int | None] = mapped_column(SqliteCompatibleBigInteger, default=None)
    batch_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default_factory=lambda: dt.now(UTC))
