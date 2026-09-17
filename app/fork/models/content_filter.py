from datetime import UTC, datetime as dt

from sqlalchemy import DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.models import IdMixin, PostgresJSONB, fk_id_column

WHOLE_NODE = ""


class ContentFilterProfile(Base, IdMixin):
    __tablename__ = "content_filter_profiles"
    __table_args__ = (UniqueConstraint("name", name="uq_content_filter_profiles_name"),)

    name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default_factory=lambda: dt.now(UTC))
    updated_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default_factory=lambda: dt.now(UTC))
    categories: Mapped[list | None] = mapped_column(PostgresJSONB, default=None)
    allow_list: Mapped[list | None] = mapped_column(PostgresJSONB, default=None)
    block_list: Mapped[list | None] = mapped_column(PostgresJSONB, default=None)
    strict_mode: Mapped[bool] = mapped_column(default=True, server_default="1")
    note: Mapped[str | None] = mapped_column(String(256), default=None)
    assignments: Mapped[list["ContentFilterAssignment"]] = relationship(  # noqa: UP037
        back_populates="profile", cascade="all, delete-orphan", init=False, lazy="selectin"
    )


class ContentFilterAssignment(Base, IdMixin):
    __tablename__ = "content_filter_assignments"
    __table_args__ = (
        UniqueConstraint("node_id", "inbound_tag", name="uq_content_filter_assignments_node_inbound"),
        Index("ix_content_filter_assignments_profile", "profile_id"),
        Index("ix_content_filter_assignments_node", "node_id"),
    )

    profile_id: Mapped[int] = fk_id_column(
        "content_filter_profiles.id", ondelete="CASCADE", name="fk_content_filter_assignments_profile"
    )
    inbound_tag: Mapped[str] = mapped_column(String(256), default=WHOLE_NODE, server_default="")
    node_id: Mapped[int | None] = fk_id_column("nodes.id", ondelete="CASCADE", nullable=True, default=None)
    is_enabled: Mapped[bool] = mapped_column(default=True, server_default="1")
    enforced: Mapped[bool] = mapped_column(default=False, server_default="0")
    last_checked_at: Mapped[dt | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    applied_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    profile: Mapped[ContentFilterProfile] = relationship(back_populates="assignments", init=False, lazy="selectin")


class ContentFilterSniffingOverride(Base, IdMixin):
    __tablename__ = "content_filter_sniffing_overrides"
    __table_args__ = (
        UniqueConstraint("core_id", "inbound_tag", name="uq_content_filter_sniffing_overrides_core_inbound"),
    )

    core_id: Mapped[int] = fk_id_column("core_configs.id", ondelete="CASCADE")
    inbound_tag: Mapped[str] = mapped_column(String(256))
    original: Mapped[dict | None] = mapped_column(PostgresJSONB, default=None)
    installed_at: Mapped[dt] = mapped_column(DateTime(timezone=True), default_factory=lambda: dt.now(UTC))
