import uuid

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class StaffRoute(TimestampMixin, Base):
    """Maps an IVR option (e.g. "sales") to a staff destination number.

    Configuration lives here, not in the Asterisk dialplan, so it can be
    changed without touching telephony config.
    """

    __tablename__ = "staff_routes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    ivr_option: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    staff_number: Mapped[str] = mapped_column(String(32))
    fallback_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Preferred outbound GSM channel for this route, if the SMG4004 supports
    # per-route channel selection. REQUIRES PHYSICAL SMG4004 to confirm.
    preferred_gsm_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    ring_timeout_seconds: Mapped[int] = mapped_column(Integer, default=25)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class GsmChannel(TimestampMixin, Base):
    """One physical GSM channel/SIM slot on the SMG4004.

    REQUIRES PHYSICAL SMG4004 — populated once the gateway is available and
    its channel/SIM layout is known; never assumed in advance beyond the
    configured channel count.
    """

    __tablename__ = "gsm_channels"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    channel_number: Mapped[int] = mapped_column(Integer, unique=True)
    sim_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    carrier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    # unknown | available | busy | offline | error
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
