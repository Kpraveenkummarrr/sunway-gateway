"""admin panel: runtime config, audit log, per-agent ring timeout

Adds what the admin panel needs to change behaviour for real:
  system_config — operator-tunable runtime settings (never secrets),
                  overlaid on the environment defaults;
  audit_logs    — who changed what in the panel;
  agents.ring_timeout_seconds — per-agent override of the department value.

Revision ID: b7e2d5c8f431
Revises: a1c4f7b2e910
Create Date: 2026-09-16 21:05:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7e2d5c8f431"
down_revision: Union[str, None] = "a1c4f7b2e910"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_config",
        sa.Column("key", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor", sa.String(length=64), nullable=False, server_default="admin"),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_audit_logs_entity_created", "audit_logs", ["entity", "created_at"])

    op.add_column("agents", sa.Column("ring_timeout_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "ring_timeout_seconds")
    op.drop_index("ix_audit_logs_entity_created", table_name="audit_logs")
    op.drop_table("audit_logs")
    op.drop_table("system_config")
