import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Department(TimestampMixin, Base):
    """One IVR menu option and the call-centre team behind it.

    Configuration lives here, not in the Asterisk dialplan, so departments,
    staff numbers and fallback behaviour can be changed from the admin API
    without touching telephony config or reloading Asterisk.
    """

    __tablename__ = "departments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    dtmf_digit: Mapped[str] = mapped_column(String(1), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Seconds to ring each destination before moving to the next one.
    ring_timeout_seconds: Mapped[int] = mapped_column(Integer, default=25)
    # What happens once every destination has been tried without an answer:
    # "ai" (hand to the AI agent) | "fallback_number" | "hangup".
    no_answer_action: Mapped[str] = mapped_column(String(32), default="ai")
    fallback_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    agents: Mapped[list["Agent"]] = relationship(
        back_populates="department", cascade="all, delete-orphan", order_by="Agent.priority"
    )


class Agent(TimestampMixin, Base):
    """A staff member who takes calls for a department.

    Destinations are tried in `priority` order (lowest first); each agent's
    backup number is tried immediately after their own number.
    """

    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    department_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("departments.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(128))
    phone_number: Mapped[str] = mapped_column(String(32))
    backup_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    priority: Mapped[int] = mapped_column(Integer, default=100)
    # Overrides the department's ring timeout for this agent when set.
    ring_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    department: Mapped["Department"] = relationship(back_populates="agents")


class GsmChannel(TimestampMixin, Base):
    """One physical GSM channel/SIM slot on the gateway.

    Populated once the gateway is available and its channel/SIM layout is
    known; never assumed in advance beyond the configured channel count.
    """

    __tablename__ = "gsm_channels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    channel_number: Mapped[int] = mapped_column(Integer, unique=True)
    sim_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    carrier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    # unknown | available | busy | offline | error
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
