"""Call-centre admin + routing API.

Two kinds of consumer:
  * the admin panel — departments, agents, call history, dashboard stats;
  * the Asterisk dialplan — GET /route/{digit}, which the AGI script calls
    to find out which numbers to ring, in order (see
    asterisk/scripts/route_agi.py).

Everything is behind admin authentication: an admin panel session cookie or
the internal API key (see app.core.admin_auth).
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.admin_auth import require_admin
from app.core.db import get_db
from app.core.logging import get_logger
from app.models.ai import AISession
from app.models.calls import Call, CallEvent
from app.models.routing import Agent, Department
from app.services.system_config import record_audit
from app.services.call_routing import (
    NO_ANSWER_ACTIONS,
    build_plan,
    get_department,
    record_routing_attempt,
    resolve_routing,
)

router = APIRouter(prefix="/api/callcentre", tags=["call centre"], dependencies=[Depends(require_admin)])
logger = get_logger(__name__)


# ---- schemas ----


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    phone_number: str = Field(min_length=1, max_length=32)
    backup_number: str | None = Field(default=None, max_length=32)
    priority: int = 100
    ring_timeout_seconds: int | None = Field(default=None, ge=5, le=120)
    active: bool = True


class AgentUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    phone_number: str | None = Field(default=None, min_length=1, max_length=32)
    backup_number: str | None = Field(default=None, max_length=32)
    priority: int | None = None
    ring_timeout_seconds: int | None = Field(default=None, ge=5, le=120)
    active: bool | None = None


class AgentOut(BaseModel):
    id: UUID
    department_id: UUID
    name: str
    phone_number: str
    backup_number: str | None
    priority: int
    ring_timeout_seconds: int | None
    active: bool

    @classmethod
    def from_model(cls, agent: Agent) -> "AgentOut":
        return cls(
            id=agent.id,
            department_id=agent.department_id,
            name=agent.name,
            phone_number=agent.phone_number,
            backup_number=agent.backup_number,
            priority=agent.priority,
            ring_timeout_seconds=agent.ring_timeout_seconds,
            active=agent.active,
        )


class DepartmentIn(BaseModel):
    dtmf_digit: str = Field(min_length=1, max_length=1, pattern=r"^[0-9]$")
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    ring_timeout_seconds: int = Field(default=25, ge=5, le=120)
    no_answer_action: str = "ai"
    fallback_number: str | None = Field(default=None, max_length=32)
    enabled: bool = True


class DepartmentUpdate(BaseModel):
    dtmf_digit: str | None = Field(default=None, min_length=1, max_length=1, pattern=r"^[0-9]$")
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    ring_timeout_seconds: int | None = Field(default=None, ge=5, le=120)
    no_answer_action: str | None = None
    fallback_number: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None


class DepartmentOut(BaseModel):
    id: UUID
    dtmf_digit: str
    name: str
    description: str | None
    ring_timeout_seconds: int
    no_answer_action: str
    fallback_number: str | None
    enabled: bool
    agents: list[AgentOut] = []

    @classmethod
    def from_model(cls, department: Department) -> "DepartmentOut":
        return cls(
            id=department.id,
            dtmf_digit=department.dtmf_digit,
            name=department.name,
            description=department.description,
            ring_timeout_seconds=department.ring_timeout_seconds,
            no_answer_action=department.no_answer_action,
            fallback_number=department.fallback_number,
            enabled=department.enabled,
            agents=[AgentOut.from_model(a) for a in sorted(department.agents, key=lambda a: (a.priority, a.name))],
        )


class DialTargetOut(BaseModel):
    number: str
    label: str
    kind: str
    agent_id: UUID | None = None


class RoutingPlanOut(BaseModel):
    department_id: UUID
    department_name: str
    dtmf_digit: str
    ring_timeout_seconds: int
    no_answer_action: str
    targets: list[DialTargetOut]


class AttemptIn(BaseModel):
    channel_id: str
    number: str
    label: str = ""
    kind: str = "agent"
    attempt: int = 1
    result: str  # answered | noanswer | busy | congestion | failed


class CallOut(BaseModel):
    id: UUID
    asterisk_channel_id: str
    direction: str
    caller_number: str | None
    called_number: str | None
    status: str
    ivr_option_selected: str | None
    routed_to_staff_number: str | None
    ai_handled: bool
    hangup_cause: str | None
    started_at: datetime | None
    answered_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None

    @classmethod
    def from_model(cls, call: Call) -> "CallOut":
        return cls(**{field: getattr(call, field) for field in cls.model_fields})


def _validate_no_answer_action(action: str) -> None:
    if action not in NO_ANSWER_ACTIONS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"no_answer_action must be one of {NO_ANSWER_ACTIONS}",
        )


async def _get_department_or_404(db: AsyncSession, department_id: UUID) -> Department:
    department = (
        await db.execute(
            select(Department).where(Department.id == department_id).options(selectinload(Department.agents))
        )
    ).scalar_one_or_none()
    if department is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Department not found")
    return department


# ---- departments ----


@router.post("/departments", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
async def create_department(
    body: DepartmentIn, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> DepartmentOut:
    _validate_no_answer_action(body.no_answer_action)
    if await get_department(db, body.dtmf_digit) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Digit {body.dtmf_digit} is already in use")

    department = Department(**body.model_dump())
    db.add(department)
    await db.commit()
    await db.refresh(department, attribute_names=["agents"])
    await record_audit(
        db, action="created", entity="department", entity_id=str(department.id),
        detail={"dtmf_digit": department.dtmf_digit, "name": department.name}, actor=actor,
    )
    logger.info("Department created: digit=%s name=%s", department.dtmf_digit, department.name)
    return DepartmentOut.from_model(department)


@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(db: AsyncSession = Depends(get_db)) -> list[DepartmentOut]:
    departments = (
        (await db.execute(select(Department).options(selectinload(Department.agents)).order_by(Department.dtmf_digit)))
        .scalars()
        .all()
    )
    return [DepartmentOut.from_model(d) for d in departments]


@router.get("/departments/{department_id}", response_model=DepartmentOut)
async def get_one_department(department_id: UUID, db: AsyncSession = Depends(get_db)) -> DepartmentOut:
    return DepartmentOut.from_model(await _get_department_or_404(db, department_id))


@router.patch("/departments/{department_id}", response_model=DepartmentOut)
async def update_department(
    department_id: UUID,
    body: DepartmentUpdate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_admin),
) -> DepartmentOut:
    department = await _get_department_or_404(db, department_id)
    changes = body.model_dump(exclude_unset=True)
    if "no_answer_action" in changes:
        _validate_no_answer_action(changes["no_answer_action"])
    if "dtmf_digit" in changes and changes["dtmf_digit"] != department.dtmf_digit:
        clash = await get_department(db, changes["dtmf_digit"])
        if clash is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Digit {changes['dtmf_digit']} is already in use")

    for attribute, value in changes.items():
        setattr(department, attribute, value)
    await db.commit()
    await db.refresh(department, attribute_names=["agents"])
    await record_audit(
        db, action="updated", entity="department", entity_id=str(department_id),
        detail={k: str(v)[:100] for k, v in changes.items()}, actor=actor,
    )
    logger.info("Department updated: id=%s fields=%s", department_id, sorted(changes))
    return DepartmentOut.from_model(department)


@router.delete("/departments/{department_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_department(
    department_id: UUID, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> None:
    department = await _get_department_or_404(db, department_id)
    await db.delete(department)
    await db.commit()
    await record_audit(db, action="deleted", entity="department", entity_id=str(department_id), actor=actor)
    logger.info("Department deleted: id=%s", department_id)


# ---- agents ----


@router.post("/departments/{department_id}/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def create_agent(
    department_id: UUID, body: AgentIn, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> AgentOut:
    await _get_department_or_404(db, department_id)
    agent = Agent(department_id=department_id, **body.model_dump())
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    await record_audit(
        db, action="created", entity="agent", entity_id=str(agent.id),
        detail={"name": agent.name, "department_id": str(department_id)}, actor=actor,
    )
    logger.info("Agent created: department=%s name=%s", department_id, agent.name)
    return AgentOut.from_model(agent)


@router.get("/agents", response_model=list[AgentOut])
async def list_agents(
    department_id: UUID | None = None, db: AsyncSession = Depends(get_db)
) -> list[AgentOut]:
    query = select(Agent).order_by(Agent.priority, Agent.name)
    if department_id is not None:
        query = query.where(Agent.department_id == department_id)
    return [AgentOut.from_model(a) for a in (await db.execute(query)).scalars().all()]


@router.patch("/agents/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: UUID, body: AgentUpdate, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> AgentOut:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Agent not found")
    changes = body.model_dump(exclude_unset=True)
    for attribute, value in changes.items():
        setattr(agent, attribute, value)
    await db.commit()
    await db.refresh(agent)
    await record_audit(
        db, action="updated", entity="agent", entity_id=str(agent_id),
        detail={k: str(v)[:100] for k, v in changes.items()}, actor=actor,
    )
    logger.info("Agent updated: id=%s fields=%s", agent_id, sorted(changes))
    return AgentOut.from_model(agent)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: UUID, db: AsyncSession = Depends(get_db), actor: str = Depends(require_admin)
) -> None:
    agent = await db.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Agent not found")
    await db.delete(agent)
    await db.commit()
    await record_audit(db, action="deleted", entity="agent", entity_id=str(agent_id), actor=actor)
    logger.info("Agent deleted: id=%s", agent_id)


# ---- routing (called by the dialplan) ----


@router.get("/route/{dtmf_digit}", response_model=RoutingPlanOut)
async def route_for_digit(dtmf_digit: str, db: AsyncSession = Depends(get_db)) -> RoutingPlanOut:
    plan = await resolve_routing(db, dtmf_digit)
    if plan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No enabled department for digit {dtmf_digit}")
    return RoutingPlanOut(
        department_id=plan.department_id,
        department_name=plan.department_name,
        dtmf_digit=plan.dtmf_digit,
        ring_timeout_seconds=plan.ring_timeout_seconds,
        no_answer_action=plan.no_answer_action,
        targets=[DialTargetOut(**vars(t)) for t in plan.targets],
    )


@router.post("/route/attempts", status_code=status.HTTP_202_ACCEPTED)
async def report_attempt(body: AttemptIn, db: AsyncSession = Depends(get_db)) -> dict:
    from app.services.call_routing import DialTarget

    event = await record_routing_attempt(
        db,
        channel_id=body.channel_id,
        target=DialTarget(number=body.number, label=body.label, kind=body.kind),
        attempt=body.attempt,
        result=body.result,
    )
    return {"recorded": event is not None}


# ---- call history + dashboard ----


@router.get("/calls", response_model=list[CallOut])
async def list_calls(
    limit: int = Query(default=50, ge=1, le=500),
    status_filter: str | None = Query(default=None, alias="status"),
    db: AsyncSession = Depends(get_db),
) -> list[CallOut]:
    query = select(Call).order_by(Call.started_at.desc().nullslast()).limit(limit)
    if status_filter:
        query = query.where(Call.status == status_filter)
    return [CallOut.from_model(c) for c in (await db.execute(query)).scalars().all()]


@router.get("/calls/{call_id}/events")
async def list_call_events(call_id: UUID, db: AsyncSession = Depends(get_db)) -> list[dict]:
    events = (
        (await db.execute(select(CallEvent).where(CallEvent.call_id == call_id).order_by(CallEvent.created_at)))
        .scalars()
        .all()
    )
    return [{"at": e.created_at, "type": e.event_type, "detail": e.detail} for e in events]


@router.get("/stats")
async def dashboard_stats(hours: int = Query(default=24, ge=1, le=720), db: AsyncSession = Depends(get_db)) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    window = Call.started_at >= since

    by_status = dict(
        (await db.execute(select(Call.status, func.count()).where(window).group_by(Call.status))).all()
    )
    total = sum(by_status.values())
    ai_calls = (await db.execute(select(func.count()).select_from(Call).where(window, Call.ai_handled.is_(True)))).scalar_one()
    staff_calls = (
        await db.execute(select(func.count()).select_from(Call).where(window, Call.routed_to_staff_number.isnot(None)))
    ).scalar_one()
    average_duration = (
        await db.execute(select(func.avg(Call.duration_seconds)).where(window, Call.duration_seconds.isnot(None)))
    ).scalar_one()
    ai_sessions = (await db.execute(select(func.count()).select_from(AISession).where(AISession.created_at >= since))).scalar_one()

    return {
        "window_hours": hours,
        "total_calls": total,
        "by_status": by_status,
        "active_calls": by_status.get("in_progress", 0),
        "ai_calls": ai_calls,
        "staff_calls": staff_calls,
        "ai_sessions": ai_sessions,
        "average_duration_seconds": round(float(average_duration), 1) if average_duration is not None else None,
    }
