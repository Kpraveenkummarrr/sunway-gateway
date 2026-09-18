"""Runtime settings the admin panel can change, overlaid on the
environment defaults.

Only operator-tunable, non-secret values are allowed (see EDITABLE_KEYS):
language, persona, speech speed, turn timeouts and caller phrases. API keys,
database URLs and provider selection stay in the environment — they are
never read from or written to the database.

The AI worker re-reads these between calls, so a change in the panel takes
effect on the next call without a restart.
"""

from dataclasses import dataclass
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.system import AuditLog, SystemConfig

logger = get_logger(__name__)


@dataclass(frozen=True)
class EditableSetting:
    key: str
    kind: type
    label: str
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None


EDITABLE_KEYS: dict[str, EditableSetting] = {
    s.key: s
    for s in (
        EditableSetting("ai_language", str, "Conversation language", choices=("hi", "en")),
        EditableSetting("ai_system_prompt", str, "System prompt (base)"),
        # "" turns the helpline rules off and leaves the base prompt alone.
        EditableSetting(
            "ai_persona", str, "Helpline persona", choices=("lsd_helpline", "")
        ),
        EditableSetting("ai_welcome_message", str, "Welcome message"),
        EditableSetting("ai_error_message", str, "Error message"),
        EditableSetting("ai_goodbye_message", str, "Goodbye message"),
        EditableSetting("ai_tts_speed", float, "Speech speed", minimum=0.8, maximum=1.6),
        EditableSetting("ai_end_of_speech_silence_seconds", int, "End-of-speech silence (s)", minimum=1, maximum=10),
        EditableSetting("ai_max_turn_seconds", int, "Max caller turn (s)", minimum=5, maximum=60),
        EditableSetting("ai_no_input_timeout_seconds", int, "No-input hangup (s)", minimum=5, maximum=120),
        EditableSetting("ai_turn_timeout_seconds", float, "AI turn timeout (s)", minimum=5, maximum=120),
        EditableSetting("ai_max_consecutive_failures", int, "Failed turns before hangup", minimum=1, maximum=10),
        EditableSetting("rag_top_k", int, "Knowledge chunks per answer", minimum=1, maximum=10),
        EditableSetting("llm_max_tokens", int, "Max answer tokens", minimum=64, maximum=1024),
    )
}


class ConfigError(ValueError):
    """A setting was unknown, or its value was rejected."""


def coerce(key: str, raw: Any) -> Any:
    spec = EDITABLE_KEYS.get(key)
    if spec is None:
        raise ConfigError(f"{key} is not an editable setting")
    try:
        value = spec.kind(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be {spec.kind.__name__}") from exc

    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{key} must be finite")
    if spec.choices and value not in spec.choices:
        raise ConfigError(f"{key} must be one of {spec.choices}")
    if spec.minimum is not None and value < spec.minimum:
        raise ConfigError(f"{key} must be at least {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise ConfigError(f"{key} must be at most {spec.maximum}")
    return value


async def load_overrides(db: AsyncSession) -> dict[str, Any]:
    """Stored overrides, coerced to their declared types. Unknown or
    unparseable rows are ignored rather than breaking the call path."""
    rows = (await db.execute(select(SystemConfig))).scalars().all()
    overrides: dict[str, Any] = {}
    for row in rows:
        try:
            overrides[row.key] = coerce(row.key, row.value)
        except ConfigError:
            logger.warning("Ignoring unusable system_config row: %s", row.key)
    return overrides


async def effective_settings(db: AsyncSession, base: Settings) -> Settings:
    overrides = await load_overrides(db)
    return base.model_copy(update=overrides) if overrides else base


async def set_values(db: AsyncSession, values: dict[str, Any], *, actor: str = "admin") -> dict[str, Any]:
    """Validates and stores settings, recording an audit entry. Returns the
    coerced values that were applied."""
    applied: dict[str, Any] = {}
    for key, raw in values.items():
        value = coerce(key, raw)
        row = await db.get(SystemConfig, key)
        if row is None:
            db.add(SystemConfig(key=key, value=str(value)))
        else:
            row.value = str(value)
        applied[key] = value

    if applied:
        db.add(
            AuditLog(
                actor=actor,
                action="updated",
                entity="ai_settings",
                detail={k: str(v)[:200] for k, v in applied.items()},
            )
        )
    await db.commit()
    logger.info("Runtime settings updated by %s: %s", actor, sorted(applied))
    return applied


async def record_audit(
    db: AsyncSession,
    *,
    action: str,
    entity: str,
    entity_id: str | None = None,
    detail: dict | None = None,
    actor: str = "admin",
) -> None:
    db.add(AuditLog(actor=actor, action=action, entity=entity, entity_id=entity_id, detail=detail))
    await db.commit()
