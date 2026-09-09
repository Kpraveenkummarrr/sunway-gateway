"""In-memory ARI test double — implements the same surface as AriClient
(app.services.ari_client) so AICallController can be tested without a
real Asterisk/ARI connection. Every call is recorded for assertions."""

import asyncio
from typing import Callable


class FakeAriClient:
    def __init__(self, *, on_play_started: Callable[[str], None] | None = None) -> None:
        self.answered: list[str] = []
        self.played: list[tuple[str, str]] = []
        self.recorded: list[tuple[str, str]] = []
        self.hungup: list[str] = []
        self.fail_answer_for: set[str] = set()
        self.fail_record_for: set[str] = set()
        # Called shortly (one event-loop tick) after each play() — real
        # Asterisk would emit a PlaybackFinished event once audio actually
        # finishes; this stands in for that so tests waiting on playback
        # completion (see AICallController._play_and_wait) don't block for
        # the real timeout. Wired to the controller's own event handler in
        # test setup, exactly as a real PlaybackFinished event would be.
        self.on_play_started = on_play_started

    async def answer(self, channel_id: str) -> None:
        if channel_id in self.fail_answer_for:
            from app.services.ari_client import AriError

            raise AriError("simulated answer failure")
        self.answered.append(channel_id)

    async def hangup(self, channel_id: str, *, reason: str | None = None) -> None:
        self.hungup.append(channel_id)

    async def play(self, channel_id: str, *, media: str) -> dict:
        self.played.append((channel_id, media))
        playback_id = f"playback-{len(self.played)}"
        if self.on_play_started is not None:
            asyncio.create_task(self._signal_playback_finished(playback_id))
        return {"id": playback_id}

    async def _signal_playback_finished(self, playback_id: str) -> None:
        await asyncio.sleep(0)  # let the caller register its wait first
        if self.on_play_started is not None:
            self.on_play_started(playback_id)

    # ---- event stream (for run_forever(), not used by dispatch_event()-based tests) ----

    async def events(self):
        if not hasattr(self, "_event_queue"):
            self._event_queue = asyncio.Queue()
        while True:
            event = await self._event_queue.get()
            if event is None:
                return
            yield event

    async def push_event(self, event: dict) -> None:
        if not hasattr(self, "_event_queue"):
            self._event_queue = asyncio.Queue()
        await self._event_queue.put(event)

    async def stop_events(self) -> None:
        if not hasattr(self, "_event_queue"):
            self._event_queue = asyncio.Queue()
        await self._event_queue.put(None)

    async def record(
        self,
        channel_id: str,
        *,
        name: str,
        max_duration_seconds: int,
        max_silence_seconds: int,
        audio_format: str = "wav",
    ) -> dict:
        if channel_id in self.fail_record_for:
            from app.services.ari_client import AriError

            raise AriError("simulated record failure")
        self.recorded.append((channel_id, name))
        return {"name": name, "format": audio_format}

    async def get_channel(self, channel_id: str) -> dict:
        return {"id": channel_id}


def stasis_start_event(channel_id: str, *, caller_number: str | None = "15551234567", exten: str = "700") -> dict:
    return {
        "type": "StasisStart",
        "channel": {
            "id": channel_id,
            "caller": {"number": caller_number},
            "dialplan": {"exten": exten},
        },
    }


def recording_finished_event(name: str, *, audio_format: str = "wav") -> dict:
    return {"type": "RecordingFinished", "recording": {"name": name, "format": audio_format}}


def hangup_event(channel_id: str) -> dict:
    return {"type": "StasisEnd", "channel": {"id": channel_id}}
