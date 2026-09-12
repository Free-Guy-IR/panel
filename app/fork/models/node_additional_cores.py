from sqlalchemy import Column, ForeignKey, Index, Table

from app.db.base import Base
from app.db.compiles_types import SqliteCompatibleBigInteger

node_additional_cores_association = Table(
    "node_additional_cores",
    Base.metadata,
    Column("node_id", SqliteCompatibleBigInteger, ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "core_config_id",
        SqliteCompatibleBigInteger,
        ForeignKey("core_configs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_node_additional_cores_core_config_id", "core_config_id"),
)
