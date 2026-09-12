from datetime import datetime as dt

from sqlalchemy import BigInteger, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models import IdMixin, fk_id_column


class NodeInboundUsage(Base, IdMixin):
    __tablename__ = "node_inbound_usages"
    __table_args__ = (
        UniqueConstraint("created_at", "node_id", "inbound_tag"),
        Index("ix_node_inbound_usages_created_at", "created_at"),
        Index("ix_node_inbound_usages_tag_created_at", "inbound_tag", "created_at"),
    )
    created_at: Mapped[dt] = mapped_column(DateTime(timezone=True), unique=False)
    node_id: Mapped[int | None] = fk_id_column("nodes.id", ondelete="SET NULL")
    inbound_tag: Mapped[str] = mapped_column(String(256))
    uplink: Mapped[int] = mapped_column(BigInteger, default=0)
    downlink: Mapped[int] = mapped_column(BigInteger, default=0)
