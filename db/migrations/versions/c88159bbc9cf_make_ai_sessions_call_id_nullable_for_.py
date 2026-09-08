"""make ai_sessions call_id nullable for text based sessions

Phase 5 adds a text/audio API for developing and testing the AI
conversation orchestration layer before any telephony integration exists.
Sessions created that way have no Call row to attach to, but
ai_sessions.call_id was NOT NULL from the Phase 2 migration (written when
AISession was assumed to always originate from a phone call). Making it
nullable lets a session exist standalone; once a session is bridged to an
actual phone call in a later phase, call_id gets populated then.

Revision ID: c88159bbc9cf
Revises: 5e5509c26b5e
Create Date: 2026-09-08 22:38:48.787049

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c88159bbc9cf"
down_revision: Union[str, None] = "5e5509c26b5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("ai_sessions", "call_id", nullable=True)


def downgrade() -> None:
    # Any standalone (call_id IS NULL) sessions created since the upgrade
    # would violate the NOT NULL constraint on downgrade — this is
    # expected; delete or backfill them before downgrading past this
    # revision in an environment that has any.
    op.alter_column("ai_sessions", "call_id", nullable=False)
