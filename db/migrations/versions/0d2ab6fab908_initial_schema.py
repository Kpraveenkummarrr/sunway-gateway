"""initial schema

Revision ID: 0d2ab6fab908
Revises:
Create Date: 2026-09-08 20:34:59.093827

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0d2ab6fab908"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMBEDDING_DIM = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "gsm_channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_number", sa.Integer(), nullable=False),
        sa.Column("sim_number", sa.String(32), nullable=True),
        sa.Column("carrier", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("channel_number"),
    )

    op.create_table(
        "staff_routes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ivr_option", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("staff_number", sa.String(32), nullable=False),
        sa.Column("fallback_number", sa.String(32), nullable=True),
        sa.Column("preferred_gsm_channel_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("ring_timeout_seconds", sa.Integer(), nullable=False, server_default="25"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("ivr_option"),
    )

    op.create_table(
        "calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("asterisk_channel_id", sa.String(128), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("caller_number", sa.String(32), nullable=True),
        sa.Column("called_number", sa.String(32), nullable=True),
        sa.Column("gsm_channel_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("gsm_channels.id"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="in_progress"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("ivr_option_selected", sa.String(32), nullable=True),
        sa.Column("routed_to_staff_number", sa.String(32), nullable=True),
        sa.Column("ai_handled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("hangup_cause", sa.String(64), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("asterisk_channel_id"),
    )
    op.create_index("ix_calls_asterisk_channel_id", "calls", ["asterisk_channel_id"])
    op.create_index("ix_calls_status_started", "calls", ["status", "started_at"])

    op.create_table(
        "call_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("calls.id"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_call_events_call_id", "call_events", ["call_id"])

    op.create_table(
        "ivr_selections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("calls.id"), nullable=False),
        sa.Column("menu_name", sa.String(64), nullable=False),
        sa.Column("digits_pressed", sa.String(16), nullable=True),
        sa.Column("resolved_option", sa.String(64), nullable=True),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("was_timeout", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("was_invalid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ivr_selections_call_id", "ivr_selections", ["call_id"])

    op.create_table(
        "recordings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("calls.id"), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("format", sa.String(16), nullable=False, server_default="wav"),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("retention_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_recordings_call_id", "recordings", ["call_id"])

    op.create_table(
        "ai_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("call_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("calls.id"), nullable=False),
        sa.Column("language", sa.String(16), nullable=False, server_default="en"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("escalated_to_staff", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("escalation_reason", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_sessions_call_id", "ai_sessions", ["call_id"])

    op.create_table(
        "ai_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("ai_sessions.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("stt_confidence", sa.Float(), nullable=True),
        sa.Column("retrieved_chunk_ids", postgresql.JSONB(), nullable=True),
        sa.Column("llm_latency_ms", sa.Integer(), nullable=True),
        sa.Column("stt_latency_ms", sa.Integer(), nullable=True),
        sa.Column("tts_latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_ai_messages_session_id", "ai_messages", ["session_id"])
    op.create_index("ix_ai_messages_session_created", "ai_messages", ["session_id", "created_at"])

    op.create_table(
        "knowledge_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=False, server_default="application/pdf"),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("knowledge_documents.id"),
            nullable=False,
        ),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_knowledge_chunks_document_id", "knowledge_chunks", ["document_id"])
    op.create_index(
        "ix_knowledge_chunks_document_chunk", "knowledge_chunks", ["document_id", "chunk_index"]
    )
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_ivfflat ON knowledge_chunks "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    op.create_table(
        "system_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("level", sa.String(16), nullable=False, server_default="INFO"),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("context", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_system_logs_event_created", "system_logs", ["event_type", "created_at"])


def downgrade() -> None:
    op.drop_table("system_logs")
    op.drop_index("ix_knowledge_chunks_embedding_ivfflat", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_document_chunk", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_document_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.drop_index("ix_ai_messages_session_created", table_name="ai_messages")
    op.drop_index("ix_ai_messages_session_id", table_name="ai_messages")
    op.drop_table("ai_messages")
    op.drop_index("ix_ai_sessions_call_id", table_name="ai_sessions")
    op.drop_table("ai_sessions")
    op.drop_index("ix_recordings_call_id", table_name="recordings")
    op.drop_table("recordings")
    op.drop_index("ix_ivr_selections_call_id", table_name="ivr_selections")
    op.drop_table("ivr_selections")
    op.drop_index("ix_call_events_call_id", table_name="call_events")
    op.drop_table("call_events")
    op.drop_index("ix_calls_status_started", table_name="calls")
    op.drop_index("ix_calls_asterisk_channel_id", table_name="calls")
    op.drop_table("calls")
    op.drop_table("staff_routes")
    op.drop_table("gsm_channels")
    op.execute("DROP EXTENSION IF EXISTS vector")
