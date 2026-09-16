"""call centre: departments and agents

Replaces the unused single-destination staff_routes table with a real
call-centre model: a department per IVR digit, and an ordered team of
agents behind it (each with a backup number, priority and active flag),
plus the department's no-answer fallback behaviour.

Revision ID: a1c4f7b2e910
Revises: c88159bbc9cf
Create Date: 2026-09-16 09:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1c4f7b2e910"
down_revision: Union[str, None] = "c88159bbc9cf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("dtmf_digit", sa.String(length=1), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("ring_timeout_seconds", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("no_answer_action", sa.String(length=32), nullable=False, server_default="ai"),
        sa.Column("fallback_number", sa.String(length=32), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("dtmf_digit", name="departments_dtmf_digit_key"),
    )
    op.create_index("ix_departments_dtmf_digit", "departments", ["dtmf_digit"])

    op.create_table(
        "agents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "department_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("phone_number", sa.String(length=32), nullable=False),
        sa.Column("backup_number", sa.String(length=32), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_agents_department_id", "agents", ["department_id"])

    op.drop_table("staff_routes")


def downgrade() -> None:
    op.create_table(
        "staff_routes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ivr_option", sa.String(length=64), nullable=False, unique=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("staff_number", sa.String(length=32), nullable=False),
        sa.Column("fallback_number", sa.String(length=32), nullable=True),
        sa.Column("preferred_gsm_channel_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ring_timeout_seconds", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.drop_index("ix_agents_department_id", table_name="agents")
    op.drop_table("agents")
    op.drop_index("ix_departments_dtmf_digit", table_name="departments")
    op.drop_table("departments")
