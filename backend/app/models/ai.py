import uuid

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class AISession(TimestampMixin, Base):
    """One AI voice-agent conversation, scoped to a single call."""

    __tablename__ = "ai_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("calls.id"), index=True)

    language: Mapped[str] = mapped_column(String(16), default="en")
    status: Mapped[str] = mapped_column(String(32), default="active")
    # active | completed | escalated | failed

    escalated_to_staff: Mapped[bool] = mapped_column(default=False)
    escalation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    call: Mapped["Call"] = relationship(back_populates="ai_sessions")  # noqa: F821
    messages: Mapped[list["AIMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class AIMessage(TimestampMixin, Base):
    """One turn in an AI session: caller utterance or agent response,
    with the pipeline stage timings/metadata that produced it."""

    __tablename__ = "ai_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ai_sessions.id"), index=True)

    role: Mapped[str] = mapped_column(String(16))  # "caller" | "agent" | "system"
    text: Mapped[str | None] = mapped_column(Text, nullable=True)

    stt_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    retrieved_chunk_ids: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    llm_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stt_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tts_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    session: Mapped["AISession"] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_ai_messages_session_created", "session_id", "created_at"),
    )
