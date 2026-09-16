"""Admin panel: authentication, runtime settings, health, audit trail, and
the panel page. Every value the panel shows comes from the backend, so the
tests assert on real reads/writes rather than on rendered markup.

The GUI itself is a static page calling these endpoints; its behaviour is
covered by asserting the endpoints it uses, plus that the page is served and
references them.
"""

import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.admin_auth import SESSION_COOKIE, issue_session, session_is_valid
from app.core.config import Settings, get_settings
from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.routing import Agent, Department
from app.models.system import AuditLog, SystemConfig
from app.services.system_config import ConfigError, coerce, effective_settings, set_values

PANEL = Path(__file__).resolve().parents[1] / "app" / "static" / "admin.html"


def _settings_with(**overrides):
    value = get_settings().model_copy(update=overrides)
    return lambda: value


def _use(**overrides):
    app.dependency_overrides[get_settings] = _settings_with(**overrides)


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
async def client():
    _use(internal_api_key="", admin_password="")  # local dev default: open
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def _clear_config(*keys: str) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(SystemConfig).where(SystemConfig.key.in_(keys)))
        await db.commit()


# ---- authentication ----


@pytest.mark.asyncio
async def test_password_login_issues_a_session_that_unlocks_the_admin_api() -> None:
    _use(internal_api_key="", admin_password="correct-horse")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get("/api/admin/settings")).status_code == 401

        wrong = await http.post("/api/admin/login", json={"password": "guess"})
        assert wrong.status_code == 401
        assert SESSION_COOKIE not in http.cookies

        good = await http.post("/api/admin/login", json={"password": "correct-horse"})
        assert good.status_code == 200
        assert SESSION_COOKIE in http.cookies

        assert (await http.get("/api/admin/settings")).status_code == 200
        # The session also unlocks the call-centre API the panel uses.
        assert (await http.get("/api/callcentre/departments")).status_code == 200

        await http.post("/api/admin/logout")
        assert (await http.get("/api/admin/settings")).status_code == 401


@pytest.mark.asyncio
async def test_api_key_still_works_for_scripts_and_the_dialplan() -> None:
    _use(internal_api_key="key-for-agi", admin_password="something")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        assert (await http.get("/api/callcentre/departments")).status_code == 401
        with_key = await http.get("/api/callcentre/departments", headers={"X-Internal-Api-Key": "key-for-agi"})
        assert with_key.status_code == 200


def test_session_cookies_are_signed_and_reject_tampering() -> None:
    settings = Settings(_env_file=None, app_secret_key="signing-key")
    token = issue_session(settings)

    assert session_is_valid(token, settings)
    assert not session_is_valid(token + "x", settings)
    assert not session_is_valid(token, Settings(_env_file=None, app_secret_key="different-key"))
    assert not session_is_valid(None, settings)


@pytest.mark.asyncio
async def test_login_reports_when_password_login_is_not_configured() -> None:
    _use(internal_api_key="only-key", admin_password="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post("/api/admin/login", json={"password": "anything"})
        assert response.status_code == 503
        assert (await http.get("/api/admin/session")).json()["password_login_enabled"] is False


# ---- runtime settings ----


@pytest.mark.asyncio
async def test_settings_expose_only_editable_non_secret_fields(client) -> None:
    body = (await client.get("/api/admin/settings")).json()

    assert "ai_tts_speed" in body["values"] and "ai_language" in body["values"]
    serialized = str(body).lower()
    for secret in ("api_key", "password", "database_url", "secret"):
        assert secret not in serialized, f"{secret} must not be exposed to the panel"


@pytest.mark.asyncio
async def test_saving_settings_persists_them_and_changes_effective_settings(client) -> None:
    try:
        response = await client.put("/api/admin/settings", json={"values": {"ai_tts_speed": "1.3", "rag_top_k": 6}})
        assert response.status_code == 200

        async with AsyncSessionLocal() as db:
            effective = await effective_settings(db, get_settings())
            assert effective.ai_tts_speed == 1.3
            assert effective.rag_top_k == 6

        reread = (await client.get("/api/admin/settings")).json()["values"]
        assert reread["ai_tts_speed"] == 1.3
    finally:
        await _clear_config("ai_tts_speed", "rag_top_k")


@pytest.mark.asyncio
async def test_settings_out_of_range_or_unknown_are_rejected(client) -> None:
    too_fast = await client.put("/api/admin/settings", json={"values": {"ai_tts_speed": "9"}})
    assert too_fast.status_code == 422

    not_editable = await client.put("/api/admin/settings", json={"values": {"llm_api_key": "sk-nope"}})
    assert not_editable.status_code == 422

    async with AsyncSessionLocal() as db:
        assert await db.get(SystemConfig, "llm_api_key") is None  # never stored


def test_coerce_enforces_types_choices_and_bounds() -> None:
    assert coerce("ai_tts_speed", "1.15") == 1.15
    assert coerce("rag_top_k", "4") == 4
    with pytest.raises(ConfigError):
        coerce("ai_language", "fr")
    with pytest.raises(ConfigError):
        coerce("ai_end_of_speech_silence_seconds", 99)
    with pytest.raises(ConfigError):
        coerce("internal_api_key", "leak")


@pytest.mark.asyncio
async def test_unusable_stored_values_are_ignored_rather_than_breaking_calls() -> None:
    async with AsyncSessionLocal() as db:
        db.add(SystemConfig(key="ai_tts_speed", value="not-a-number"))
        await db.commit()
    try:
        async with AsyncSessionLocal() as db:
            effective = await effective_settings(db, get_settings())
        assert effective.ai_tts_speed == get_settings().ai_tts_speed  # fell back to the env default
    finally:
        await _clear_config("ai_tts_speed")


# ---- audit ----


@pytest.mark.asyncio
async def test_configuration_changes_are_audited(client) -> None:
    digit = str(uuid.uuid4().int % 10)
    created = await client.post("/api/callcentre/departments", json={"dtmf_digit": digit, "name": "Audited dept"})
    department_id = created.json()["id"]
    try:
        await client.put("/api/admin/settings", json={"values": {"ai_tts_speed": "1.2"}})
        trail = (await client.get("/api/admin/audit?limit=20")).json()

        entities = [(row["entity"], row["action"]) for row in trail]
        assert ("department", "created") in entities
        assert ("ai_settings", "updated") in entities
        assert "sk-" not in str(trail)
    finally:
        await client.delete(f"/api/callcentre/departments/{department_id}")
        await _clear_config("ai_tts_speed")
        async with AsyncSessionLocal() as db:
            await db.execute(delete(AuditLog).where(AuditLog.entity.in_(["department", "ai_settings"])))
            await db.commit()


# ---- health, routing overview, panel ----


@pytest.mark.asyncio
async def test_health_reports_every_service_and_never_invents_values(client) -> None:
    body = (await client.get("/api/admin/health")).json()

    assert set(body["checks"]) == {"asterisk", "ai_worker", "gateway_sip", "resources"}
    for name, check in body["checks"].items():
        assert check["ok"] in (True, False, None), name
        assert check["detail"], f"{name} must explain its state"
    # Unreachable ARI must be reported, not guessed.
    assert body["checks"]["asterisk"]["ok"] in (True, False)


@pytest.mark.asyncio
async def test_routing_overview_shows_departments_and_reserved_digits(client) -> None:
    digit = "3"
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(Department).where(Department.dtmf_digit == digit))).scalar_one_or_none()
        if existing is not None:
            await db.execute(delete(Agent).where(Agent.department_id == existing.id))
            await db.delete(existing)
            await db.commit()

    created = await client.post("/api/callcentre/departments", json={"dtmf_digit": digit, "name": "Billing"})
    department_id = created.json()["id"]
    await client.post(
        f"/api/callcentre/departments/{department_id}/agents",
        json={"name": "Amit", "phone_number": "+915550001", "priority": 5},
    )
    try:
        digits = (await client.get("/api/admin/routing-overview")).json()["digits"]

        assert digits[digit]["name"] == "Billing"
        assert digits[digit]["destinations"][0]["number"] == "+915550001"
        assert digits["9"]["kind"] == "reserved" and "AI" in digits["9"]["name"]
        assert digits["0"]["kind"] == "reserved"
    finally:
        await client.delete(f"/api/callcentre/departments/{department_id}")


@pytest.mark.asyncio
async def test_agent_ring_timeout_override_round_trips(client) -> None:
    digit = str(uuid.uuid4().int % 10)
    created = await client.post("/api/callcentre/departments", json={"dtmf_digit": digit, "name": "Override dept"})
    department_id = created.json()["id"]
    try:
        agent = await client.post(
            f"/api/callcentre/departments/{department_id}/agents",
            json={"name": "Fast", "phone_number": "+915550009", "ring_timeout_seconds": 12},
        )
        assert agent.json()["ring_timeout_seconds"] == 12
        listed = (await client.get(f"/api/callcentre/agents?department_id={department_id}")).json()
        assert listed[0]["ring_timeout_seconds"] == 12
    finally:
        await client.delete(f"/api/callcentre/departments/{department_id}")


@pytest.mark.asyncio
async def test_panel_page_is_served_and_uses_the_real_endpoints(client) -> None:
    response = await client.get("/api/admin/panel")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

    page = PANEL.read_text(encoding="utf-8")
    for endpoint in (
        "/api/callcentre/stats",
        "/api/callcentre/departments",
        "/api/callcentre/agents",
        "/api/callcentre/calls",
        "/api/knowledge",
        "/api/admin/health",
        "/api/admin/settings",
        "/api/admin/routing-overview",
        "/api/admin/audit",
    ):
        assert endpoint in page, f"panel must call {endpoint}"
    assert "Math.random" not in page and "faker" not in page.lower()  # no invented numbers


# ---- the change actually reaches a call ----


@pytest.mark.asyncio
async def test_settings_saved_in_the_panel_reach_the_next_call() -> None:
    """The worker re-reads runtime settings per call, so a speech-speed
    change made in the panel applies without a restart."""
    from tests.fake_ari import FakeAriClient
    from app.providers.embeddings.mock import MockEmbeddingProvider
    from app.providers.llm.mock import MockLLMProvider
    from app.providers.stt.mock import MockSTTProvider
    from app.providers.tts.mock import MockTTSProvider
    from app.services.call_controller import AICallController

    base = get_settings().model_copy(update={"ai_tts_speed": 1.0})
    controller = AICallController(
        ari=FakeAriClient(),
        settings=base,
        session_factory=AsyncSessionLocal,
        embedding_provider=MockEmbeddingProvider(dimensions=1536),
        llm_provider=MockLLMProvider(),
        stt_provider=MockSTTProvider(),
        tts_provider=MockTTSProvider(),
    )
    assert controller._settings.ai_tts_speed == 1.0
    try:
        async with AsyncSessionLocal() as db:
            await set_values(db, {"ai_tts_speed": 1.25}, actor="test")

        await controller._refresh_settings()

        assert controller._settings.ai_tts_speed == 1.25
        assert controller._phrase_clips == {}  # cached phrases dropped so nothing stale is played
    finally:
        await _clear_config("ai_tts_speed")
        async with AsyncSessionLocal() as db:
            await db.execute(delete(AuditLog).where(AuditLog.actor == "test"))
            await db.commit()
