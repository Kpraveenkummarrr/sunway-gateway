"""Call-centre routing and admin API.

Routing decides who a caller reaches, so the ordering rules (priority,
backups, inactive agents, disabled departments, fallback) are pinned here.
Tests run against the real dev database and clean up after themselves.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.main import app
from app.models.calls import Call, CallEvent
from app.models.routing import Agent, Department
from app.services.call_routing import build_plan, record_routing_attempt, resolve_routing


def _digit() -> str:
    """Departments are unique per digit; tests pick their own to stay isolated."""
    return str(uuid.uuid4().int % 10)


async def _make_department(db, *, digit: str, agents: list[dict], **overrides) -> Department:
    department = Department(
        dtmf_digit=digit,
        name=overrides.pop("name", f"Dept {digit}"),
        ring_timeout_seconds=overrides.pop("ring_timeout_seconds", 20),
        no_answer_action=overrides.pop("no_answer_action", "ai"),
        **overrides,
    )
    db.add(department)
    await db.flush()
    for spec in agents:
        db.add(Agent(department_id=department.id, **spec))
    await db.commit()
    await db.refresh(department, attribute_names=["agents"])
    return department


async def _cleanup(*digits: str) -> None:
    async with AsyncSessionLocal() as db:
        for digit in digits:
            department = (
                await db.execute(select(Department).where(Department.dtmf_digit == digit))
            ).scalar_one_or_none()
            if department is not None:
                await db.execute(delete(Agent).where(Agent.department_id == department.id))
                await db.delete(department)
        await db.commit()


# ---- routing rules ----


@pytest.mark.asyncio
async def test_agents_are_tried_by_priority_each_followed_by_their_backup(db_session) -> None:
    digit = _digit()
    try:
        department = await _make_department(
            db_session,
            digit=digit,
            agents=[
                {"name": "Priya", "phone_number": "+915550002", "backup_number": "+915550022", "priority": 20},
                {"name": "Rahul", "phone_number": "+915550001", "backup_number": "+915550011", "priority": 10},
            ],
        )

        plan = build_plan(department)

        assert [(t.number, t.kind) for t in plan.targets] == [
            ("+915550001", "agent"),
            ("+915550011", "agent_backup"),
            ("+915550002", "agent"),
            ("+915550022", "agent_backup"),
        ]
        assert plan.ring_timeout_seconds == 20
        assert plan.is_reachable
    finally:
        await _cleanup(digit)


@pytest.mark.asyncio
async def test_inactive_agents_are_skipped_and_missing_backups_are_fine(db_session) -> None:
    digit = _digit()
    try:
        department = await _make_department(
            db_session,
            digit=digit,
            agents=[
                {"name": "OnLeave", "phone_number": "+915550003", "priority": 10, "active": False},
                {"name": "OnDuty", "phone_number": "+915550004", "priority": 20},
            ],
        )

        assert [t.number for t in build_plan(department).targets] == ["+915550004"]
    finally:
        await _cleanup(digit)


@pytest.mark.asyncio
async def test_fallback_number_is_a_target_only_when_the_action_uses_it(db_session) -> None:
    digit = _digit()
    try:
        department = await _make_department(
            db_session,
            digit=digit,
            agents=[{"name": "Amit", "phone_number": "+915550005"}],
            no_answer_action="fallback_number",
            fallback_number="+915559999",
        )
        assert [t.kind for t in build_plan(department).targets] == ["agent", "department_fallback"]

        department.no_answer_action = "ai"
        await db_session.commit()
        assert [t.kind for t in build_plan(department).targets] == ["agent"]
    finally:
        await _cleanup(digit)


@pytest.mark.asyncio
async def test_disabled_department_and_unknown_digit_have_no_plan(db_session) -> None:
    digit = _digit()
    try:
        await _make_department(
            db_session, digit=digit, agents=[{"name": "Anyone", "phone_number": "+915550006"}], enabled=False
        )

        assert await resolve_routing(db_session, digit) is None
        assert await resolve_routing(db_session, "7" if digit != "7" else "6") is None
    finally:
        await _cleanup(digit)


@pytest.mark.asyncio
async def test_department_with_no_active_agents_is_unreachable(db_session) -> None:
    digit = _digit()
    try:
        await _make_department(
            db_session,
            digit=digit,
            agents=[{"name": "OnLeave", "phone_number": "+915550007", "active": False}],
        )
        plan = await resolve_routing(db_session, digit)
        assert plan is not None and not plan.is_reachable  # dialplan plays "unavailable"
    finally:
        await _cleanup(digit)


# ---- attempt logging ----


@pytest.mark.asyncio
async def test_answered_attempt_is_recorded_against_the_call(db_session) -> None:
    from app.services.call_routing import DialTarget

    channel = f"PJSIP/cc-{uuid.uuid4().hex[:10]}"
    call = Call(asterisk_channel_id=channel, direction="inbound", status="in_progress")
    db_session.add(call)
    await db_session.commit()
    try:
        await record_routing_attempt(
            db_session,
            channel_id=channel,
            target=DialTarget(number="+915550001", label="Rahul", kind="agent"),
            attempt=1,
            result="noanswer",
        )
        await record_routing_attempt(
            db_session,
            channel_id=channel,
            target=DialTarget(number="+915550011", label="Rahul (backup)", kind="agent_backup"),
            attempt=2,
            result="answered",
        )

        events = (
            (await db_session.execute(select(CallEvent).where(CallEvent.call_id == call.id).order_by(CallEvent.created_at)))
            .scalars()
            .all()
        )
        assert [e.detail["result"] for e in events] == ["noanswer", "answered"]
        await db_session.refresh(call)
        assert call.routed_to_staff_number == "+915550011"
        assert call.status == "answered_by_staff"
    finally:
        await db_session.execute(delete(CallEvent).where(CallEvent.call_id == call.id))
        await db_session.delete(call)
        await db_session.commit()


@pytest.mark.asyncio
async def test_attempt_for_an_unknown_channel_is_ignored(db_session) -> None:
    from app.services.call_routing import DialTarget

    assert (
        await record_routing_attempt(
            db_session,
            channel_id="PJSIP/never-seen",
            target=DialTarget(number="+910000000", label="x", kind="agent"),
            attempt=1,
            result="answered",
        )
        is None
    )


# ---- admin API ----


def _settings_with(**overrides):
    overridden = get_settings().model_copy(update=overrides)
    return lambda: overridden


@pytest.fixture
async def client():
    # Unauthenticated, same as the local dev default; enforcement is covered
    # by test_admin_api_requires_the_internal_api_key below.
    app.dependency_overrides[get_settings] = _settings_with(internal_api_key="")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            yield http
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_admin_api_requires_the_internal_api_key() -> None:
    app.dependency_overrides[get_settings] = _settings_with(internal_api_key="test-secret-key")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.get("/api/callcentre/departments")).status_code == 401
            with_key = await http.get(
                "/api/callcentre/departments", headers={"X-Internal-Api-Key": "test-secret-key"}
            )
            assert with_key.status_code == 200
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.mark.asyncio
async def test_department_and_agent_lifecycle_through_the_api(client) -> None:
    digit = _digit()
    try:
        created = await client.post(
            "/api/callcentre/departments",
            json={"dtmf_digit": digit, "name": "Lumpy disease helpdesk", "ring_timeout_seconds": 30},
        )
        assert created.status_code == 201, created.text
        department_id = created.json()["id"]

        duplicate = await client.post("/api/callcentre/departments", json={"dtmf_digit": digit, "name": "Clash"})
        assert duplicate.status_code == 409

        agent = await client.post(
            f"/api/callcentre/departments/{department_id}/agents",
            json={"name": "Dr Singh", "phone_number": "+915551234", "backup_number": "+915555678", "priority": 10},
        )
        assert agent.status_code == 201, agent.text

        plan = await client.get(f"/api/callcentre/route/{digit}")
        assert plan.status_code == 200
        body = plan.json()
        assert body["ring_timeout_seconds"] == 30
        assert [t["number"] for t in body["targets"]] == ["+915551234", "+915555678"]

        deactivated = await client.patch(f"/api/callcentre/agents/{agent.json()['id']}", json={"active": False})
        assert deactivated.status_code == 200
        assert (await client.get(f"/api/callcentre/route/{digit}")).json()["targets"] == []

        disabled = await client.patch(f"/api/callcentre/departments/{department_id}", json={"enabled": False})
        assert disabled.status_code == 200
        assert (await client.get(f"/api/callcentre/route/{digit}")).status_code == 404

        assert (await client.delete(f"/api/callcentre/departments/{department_id}")).status_code == 204
        assert (await client.get(f"/api/callcentre/departments/{department_id}")).status_code == 404
    finally:
        await _cleanup(digit)


@pytest.mark.asyncio
async def test_api_rejects_unknown_no_answer_action(client) -> None:
    response = await client.post(
        "/api/callcentre/departments",
        json={"dtmf_digit": _digit(), "name": "Bad action", "no_answer_action": "teleport"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_stats_endpoint_reports_call_counters(client) -> None:
    response = await client.get("/api/callcentre/stats?hours=24")
    assert response.status_code == 200
    body = response.json()
    assert {"total_calls", "by_status", "active_calls", "ai_calls", "staff_calls"} <= body.keys()
