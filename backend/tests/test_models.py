from app.models import Base

EXPECTED_TABLES = {
    "calls",
    "call_events",
    "ivr_selections",
    "ai_sessions",
    "ai_messages",
    "recordings",
    "departments",
    "agents",
    "gsm_channels",
    "knowledge_documents",
    "knowledge_chunks",
    "system_logs",
}


def test_all_expected_tables_are_registered() -> None:
    assert EXPECTED_TABLES.issubset(set(Base.metadata.tables.keys()))
