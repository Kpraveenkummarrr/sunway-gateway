import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin

# Embedding dimension is fixed at the database level (pgvector requires a
# fixed vector size per column). 1536 matches common embedding models
# (e.g. OpenAI text-embedding-3-small/ada-002). RAG_EMBEDDING_MODEL is not
# yet chosen (see .env.example) — if the eventual provider uses a different
# dimension, this requires a migration to change the column type.
EMBEDDING_DIM = 1536


class KnowledgeDocument(TimestampMixin, Base):
    """One uploaded PDF/document used as AI knowledge-base source material."""

    __tablename__ = "knowledge_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(64), default="application/pdf")
    storage_path: Mapped[str] = mapped_column(Text)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    # pending | processing | ready | failed
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class KnowledgeChunk(TimestampMixin, Base):
    """One embedded chunk of text extracted from a knowledge document."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_documents.id"), index=True
    )

    page_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    chunk_text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    document: Mapped["KnowledgeDocument"] = relationship(back_populates="chunks")

    __table_args__ = (
        Index("ix_knowledge_chunks_document_chunk", "document_id", "chunk_index"),
        Index(
            "ix_knowledge_chunks_embedding_ivfflat",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
