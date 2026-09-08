import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Call(TimestampMixin, Base):
    """One phone call, GSM-inbound or GSM-outbound, from the moment
    Asterisk sees the channel to hangup."""

    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Asterisk channel/uniqueid this call corresponds to.
    asterisk_channel_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)

    direction: Mapped[str] = mapped_column(String(16))  # "inbound" | "outbound"
    caller_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    called_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Which SMG4004 GSM channel/SIM handled this call, if known.
    # REQUIRES PHYSICAL SMG4004 to populate reliably.
    gsm_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gsm_channels.id"), nullable=True
    )

    status: Mapped[str] = mapped_column(String(32), default="in_progress")
    # in_progress | completed | failed | no_answer | busy | abandoned

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    ivr_option_selected: Mapped[str | None] = mapped_column(String(32), nullable=True)
    routed_to_staff_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_handled: Mapped[bool] = mapped_column(default=False)

    hangup_cause: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    events: Mapped[list["CallEvent"]] = relationship(back_populates="call", cascade="all, delete-orphan")
    ivr_selections: Mapped[list["IVRSelection"]] = relationship(back_populates="call", cascade="all, delete-orphan")
    ai_sessions: Mapped[list["AISession"]] = relationship(back_populates="call", cascade="all, delete-orphan")
    recordings: Mapped[list["Recording"]] = relationship(back_populates="call", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_calls_status_started", "status", "started_at"),
    )


class CallEvent(TimestampMixin, Base):
    """Structured timeline of what happened during a call, e.g.
    CALL_STARTED, IVR_SELECTED, CALL_TRANSFERRED, CALL_ENDED, CALL_FAILED."""

    __tablename__ = "call_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("calls.id"), index=True)

    event_type: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    call: Mapped["Call"] = relationship(back_populates="events")


class IVRSelection(TimestampMixin, Base):
    __tablename__ = "ivr_selections"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("calls.id"), index=True)

    menu_name: Mapped[str] = mapped_column(String(64))
    digits_pressed: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolved_option: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    was_timeout: Mapped[bool] = mapped_column(default=False)
    was_invalid: Mapped[bool] = mapped_column(default=False)

    call: Mapped["Call"] = relationship(back_populates="ivr_selections")


class Recording(TimestampMixin, Base):
    __tablename__ = "recordings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    call_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("calls.id"), index=True)

    file_path: Mapped[str] = mapped_column(Text)
    format: Mapped[str] = mapped_column(String(16), default="wav")
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    call: Mapped["Call"] = relationship(back_populates="recordings")
