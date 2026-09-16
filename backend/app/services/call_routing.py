"""Call-centre routing: turns an IVR digit into an ordered list of numbers
to ring, and records what happened on each attempt.

Ordering rule (documented so the dialplan and the admin UI agree):
    agents sorted by priority (lowest first), then name;
    each agent's own number is tried first, then their backup number;
    when every destination has been tried, the department's
    `no_answer_action` decides what happens next.

The Asterisk dialplan asks for this plan over the internal API (see
asterisk/scripts/route_agi.py) rather than hard-coding numbers, so routing
can be changed from the admin panel without touching telephony config.
"""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.calls import Call, CallEvent
from app.models.routing import Department

NO_ANSWER_ACTIONS = ("ai", "fallback_number", "hangup")


@dataclass(frozen=True)
class DialTarget:
    number: str
    label: str  # who/what this number belongs to, for logs and the admin UI
    kind: str  # "agent" | "agent_backup" | "department_fallback"
    agent_id: uuid.UUID | None = None


@dataclass(frozen=True)
class RoutingPlan:
    department_id: uuid.UUID
    department_name: str
    dtmf_digit: str
    ring_timeout_seconds: int
    no_answer_action: str
    targets: list[DialTarget] = field(default_factory=list)

    @property
    def is_reachable(self) -> bool:
        return bool(self.targets)


async def get_department(db: AsyncSession, dtmf_digit: str) -> Department | None:
    result = await db.execute(
        select(Department)
        .where(Department.dtmf_digit == dtmf_digit)
        .options(selectinload(Department.agents))
    )
    return result.scalar_one_or_none()


def build_plan(department: Department) -> RoutingPlan:
    """Ordered dial targets for a department. Inactive agents are skipped;
    the department fallback number is only a target when the no-answer
    action actually uses it."""
    targets: list[DialTarget] = []
    for agent in sorted(department.agents, key=lambda a: (a.priority, a.name)):
        if not agent.active:
            continue
        targets.append(DialTarget(number=agent.phone_number, label=agent.name, kind="agent", agent_id=agent.id))
        if agent.backup_number:
            targets.append(
                DialTarget(
                    number=agent.backup_number,
                    label=f"{agent.name} (backup)",
                    kind="agent_backup",
                    agent_id=agent.id,
                )
            )

    if department.no_answer_action == "fallback_number" and department.fallback_number:
        targets.append(
            DialTarget(number=department.fallback_number, label=f"{department.name} fallback", kind="department_fallback")
        )

    return RoutingPlan(
        department_id=department.id,
        department_name=department.name,
        dtmf_digit=department.dtmf_digit,
        ring_timeout_seconds=department.ring_timeout_seconds,
        no_answer_action=department.no_answer_action,
        targets=targets,
    )


async def resolve_routing(db: AsyncSession, dtmf_digit: str) -> RoutingPlan | None:
    """The plan for a digit, or None if no enabled department owns it."""
    department = await get_department(db, dtmf_digit)
    if department is None or not department.enabled:
        return None
    return build_plan(department)


async def record_call_event(
    db: AsyncSession, call_id: uuid.UUID, event_type: str, detail: dict | None = None
) -> CallEvent:
    event = CallEvent(call_id=call_id, event_type=event_type, detail=detail)
    db.add(event)
    await db.commit()
    await db.refresh(event)
    return event


async def record_routing_attempt(
    db: AsyncSession,
    *,
    channel_id: str,
    target: DialTarget,
    attempt: int,
    result: str,
) -> CallEvent | None:
    """Logs one dial attempt against the call with this Asterisk channel id.
    Returns None when the channel has no Call row (e.g. a softphone test
    call that never went through the AI worker)."""
    call = (
        await db.execute(select(Call).where(Call.asterisk_channel_id == channel_id))
    ).scalar_one_or_none()
    if call is None:
        return None

    if result == "answered":
        call.routed_to_staff_number = target.number
        call.status = "answered_by_staff"

    return await record_call_event(
        db,
        call.id,
        "routing_attempt",
        {
            "attempt": attempt,
            "number": target.number,
            "label": target.label,
            "kind": target.kind,
            "agent_id": str(target.agent_id) if target.agent_id else None,
            "result": result,
        },
    )
