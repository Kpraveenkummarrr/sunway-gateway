"""Noise or a caller's "hello" in the first second must not cut the welcome (client logs: every call's
welcome stopped at ~1.0 s with outcome=barge-in)."""
import tempfile
import uuid
from pathlib import Path

import pytest

from app.services.call_controller import CallState
from tests.fake_ari import talking_started_event
from tests.test_call_controller import _make_controller


@pytest.mark.asyncio
@pytest.mark.parametrize("interruptible, stopped", [(False, 0), (True, 1)])
async def test_welcome_is_protected_unless_configured(interruptible, stopped) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_welcome_interruptible=interruptible)
        state = CallState("PJSIP/700-W", uuid.uuid4(), uuid.uuid4())
        controller._calls[state.channel_id] = state
        state.welcome_playing = not interruptible
        state.active_playback_id = "pb-1"
        await controller.dispatch_event(talking_started_event(state.channel_id))
        assert len(ari.stopped_playbacks) == stopped
