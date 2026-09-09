"""In-memory ARI test double — implements the same surface as AriClient
(app.services.ari_client) so AICallController can be tested without a
real Asterisk/ARI connection. Every call is recorded for assertions."""


class FakeAriClient:
    def __init__(self) -> None:
        self.answered: list[str] = []
        self.played: list[tuple[str, str]] = []
        self.recorded: list[tuple[str, str]] = []
        self.hungup: list[str] = []
        self.fail_answer_for: set[str] = set()
        self.fail_record_for: set[str] = set()

    async def answer(self, channel_id: str) -> None:
        if channel_id in self.fail_answer_for:
            from app.services.ari_client import AriError

            raise AriError("simulated answer failure")
        self.answered.append(channel_id)

    async def hangup(self, channel_id: str, *, reason: str | None = None) -> None:
        self.hungup.append(channel_id)

    async def play(self, channel_id: str, *, media: str) -> dict:
        self.played.append((channel_id, media))
        return {"id": f"playback-{len(self.played)}"}

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
