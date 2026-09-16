import uuid

from sqlalchemy import Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SystemLog(TimestampMixin, Base):
    """Structured application/event log persisted for audit and
    troubleshooting. Never contains API keys, SIP passwords, or DB
    credentials — callers are responsible for not passing secrets in."""

    __tablename__ = "system_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    level: Mapped[str] = mapped_column(String(16), default="INFO")
    source: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_system_logs_event_created", "event_type", "created_at"),
    )


class SystemConfig(TimestampMixin, Base):
    """Runtime settings changed from the admin panel, overlaid on the
    environment defaults (see app.services.system_config).

    Only non-secret, operator-tunable values belong here — language,
    persona, speech speed, timeouts. API keys and credentials stay in the
    environment and are never written to this table.
    """

    __tablename__ = "system_config"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class AuditLog(TimestampMixin, Base):
    """Who changed what in the admin panel. `actor` is the admin identity
    (session or API key), never a credential value."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    actor: Mapped[str] = mapped_column(String(64), default="admin")
    action: Mapped[str] = mapped_column(String(64))  # created | updated | deleted
    entity: Mapped[str] = mapped_column(String(64))  # department | agent | ai_settings | ...
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (Index("ix_audit_logs_entity_created", "entity", "created_at"),)
