import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from querywise_mcp.db.base import Base
from querywise_mcp.db.types import Embedding


class GlossaryTerm(Base):
    __tablename__ = "glossary_terms"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("database_connections.id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[str] = mapped_column(String(255), nullable=False)
    definition: Mapped[str] = mapped_column(Text, nullable=False)
    sql_expression: Mapped[str] = mapped_column(Text, nullable=False)
    related_tables: Mapped[list[str] | None] = mapped_column(JSON)
    related_columns: Mapped[list[str] | None] = mapped_column(JSON)
    examples: Mapped[list | None] = mapped_column(JSON, default=list)
    term_embedding = mapped_column(Embedding, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    connection: Mapped["DatabaseConnection"] = relationship(  # noqa: F821
        back_populates="glossary_terms"
    )
