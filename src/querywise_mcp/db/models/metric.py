import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from querywise_mcp.db.base import Base
from querywise_mcp.db.types import Embedding


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("database_connections.id", ondelete="CASCADE"), nullable=False
    )
    metric_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    sql_expression: Mapped[str] = mapped_column(Text, nullable=False)
    aggregation_type: Mapped[str | None] = mapped_column(String(50))
    related_tables: Mapped[list[str] | None] = mapped_column(JSON)
    dimensions: Mapped[list[str] | None] = mapped_column(JSON)
    filters: Mapped[dict | None] = mapped_column(JSON, default=dict)
    metric_embedding = mapped_column(Embedding, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    connection: Mapped["DatabaseConnection"] = relationship(  # noqa: F821
        back_populates="metric_definitions"
    )
