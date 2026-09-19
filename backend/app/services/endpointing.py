"""End-of-speech detection that does not depend on how the gateway behaves.

Why this exists
---------------
The caller's turn is recorded with ARI `record`, and Asterisk ends it when its
own silence detector has seen `maxSilenceSeconds` of silence. That detector is
driven by *voice frames*. A gateway with silence suppression / comfort noise
enabled (the client's Synway sends RTP payload type 13 during silence) stops
sending voice frames the moment the caller stops talking, so the detector's
clock never advances: the recording only ends at `maxDurationSeconds`. Measured
on Asterisk 18.10 with identical audio, that turned a 1.2 s wait into 16.9 s
(see docs/LATENCY_ROOT_CAUSE.md).

What is NOT used: the recording file
------------------------------------
The first version of this module followed the WAV file as Asterisk wrote it.
That is unsound and was removed: Asterisk 18.10 writes the recording in 32 KiB
blocks (2.05 s of audio at a time - measured on a live call: 44 bytes for the
first 2.03 s, then 32,812). "The file has not grown for 1.2 s" therefore says
nothing about the caller, and that version cut callers off mid-sentence (an
utterance of 2.76 s was ended at 2.1 s).

What is used: two real-time signals Asterisk already provides
-------------------------------------------------------------
1. TALK_DETECT events. `ChannelTalkingStarted` marks speech; Asterisk sends
   `ChannelTalkingFinished` once it has heard `talk_detect_silence_ms` of quiet
   voice frames (measured: 170 ms after the last speech frame). The worker adds
   the rest of the wait on its own clock, so nothing here depends on the
   gateway continuing to send. It works when the gateway sends continuous
   frames, and when it sends comfort noise after a hangover of at least
   `talk_detect_silence_ms` of voice frames.

2. RTP receive counters (ARI rtp_statistics). A G.711 voice packet carries 80-240
   bytes and an RFC 3389 comfort-noise packet 1-13, so packets-per-byte says
   whether *voice frames are still arriving*, whatever the gateway's hangover
   and whatever the DSP threshold. It covers exactly the case (1) cannot: a
   gateway that goes from speech straight to comfort noise / silence, for which
   Asterisk never sends "finished" at all (measured: with 0 ms or 100 ms of
   hangover no event arrives).

Whichever fires first ends the turn. Asterisk's own silence and duration limits
stay in place as a backstop.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class EndpointConfig:
    silence_ms: int = 1200  # no speech for this long, after speech, ends the turn
    min_speech_ms: int = 250  # speech shorter than this is a click/echo, not a turn
    talk_detect_silence_ms: int = 200  # Asterisk reports 'finished' this long after the last speech
    use_rtp_statistics: bool = True
    start_grace_ms: int = 200  # ignore RTP in the first moments: the prompt just played can echo
    voice_packet_bytes: int = 32  # mean payload above this = voice frames (G.711: 80-240); below = comfort noise (1-13)
    tick_seconds: float = 0.1
    rtp_poll_seconds: float = 0.2


@dataclass(frozen=True)
class EndDecision:
    reason: str  # which signal ended the turn
    last_speech_at: float  # clock value when speech was last known to be present
    speech_ms: int


REASON_TALK_DETECT = "TALK_DETECT finished + worker timer"
REASON_NO_VOICE = "no voice frames (RTP statistics)"


class EndOfSpeechTracker:
    """Pure decision logic for one caller turn: feed it events and counters,
    ask it whether the caller has finished. The clock is passed in, so it is
    testable without sleeping."""

    def __init__(self, config: EndpointConfig, now: float, *, already_talking: bool = False) -> None:
        self.config = config
        self.started_at = now
        self._talking = already_talking
        self._talk_started_at: float | None = now if already_talking else None
        self._spoken_ms = 0.0
        self._finished_at: float | None = None
        self._prev_counters: tuple[float, int, int] | None = None
        self._voice_ms = 0.0
        self._last_voice_at: float | None = None

    # ---- TALK_DETECT ----

    def talking_started(self, now: float) -> None:
        if not self._talking:
            self._talking = True
            self._talk_started_at = now
        self._finished_at = None

    def talking_finished(self, now: float, duration_ms: int | float | None = None) -> None:
        """`duration_ms` is the talking time Asterisk reports with the event."""
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            spell_ms = float(duration_ms)
        elif self._talk_started_at is not None:
            spell_ms = max(0.0, (now - self._talk_started_at) * 1000 - self.config.talk_detect_silence_ms)
        else:
            spell_ms = 0.0
        self._spoken_ms += spell_ms
        self._talking = False
        self._talk_started_at = None
        self._finished_at = now

    # ---- RTP receive counters ----

    def rtp_counters(self, now: float, packets: int, octets: int) -> None:
        """Feed the cumulative RTP receive counters. Voice-like traffic since
        the previous sample means the gateway is still sending voice frames."""
        previous, self._prev_counters = self._prev_counters, (now, packets, octets)
        if previous is None or not self.config.use_rtp_statistics:
            return
        prev_time, prev_packets, prev_octets = previous
        delta_packets, delta_octets = packets - prev_packets, octets - prev_octets
        if delta_packets <= 0 or delta_octets / delta_packets < self.config.voice_packet_bytes:
            return
        if (now - self.started_at) * 1000 < self.config.start_grace_ms:
            return  # echo of the prompt that just ended
        self._voice_ms += min(now - prev_time, 1.0) * 1000
        self._last_voice_at = now

    # ---- decision ----

    @property
    def speech_ms(self) -> int:
        """Talking time, as Asterisk measured it when it reported any; otherwise
        the time voice frames were arriving (which, for a gateway that sends
        frames all the time, includes silence - so it is only the fallback)."""
        return int(self._spoken_ms or self._voice_ms)

    def decision(self, now: float) -> EndDecision | None:
        cfg = self.config
        if not self._talking and self._finished_at is not None and self._spoken_ms >= cfg.min_speech_ms:
            # 'finished' is reported talk_detect_silence_ms after the last speech,
            # so that part of the quiet has already elapsed when it arrives.
            quiet_ms = (now - self._finished_at) * 1000 + cfg.talk_detect_silence_ms
            if quiet_ms >= cfg.silence_ms:
                return EndDecision(
                    REASON_TALK_DETECT, self._finished_at - cfg.talk_detect_silence_ms / 1000, int(self._spoken_ms)
                )
        if self._last_voice_at is not None and self._voice_ms >= cfg.min_speech_ms:
            if (now - self._last_voice_at) * 1000 >= cfg.silence_ms:
                return EndDecision(REASON_NO_VOICE, self._last_voice_at, int(self._voice_ms))
        return None


async def monitor_turn(
    tracker: EndOfSpeechTracker,
    *,
    is_current: Callable[[], bool],
    on_end: Callable[[EndDecision], Awaitable[None]],
    sample_rtp: Callable[[], Awaitable[tuple[int, int] | None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> EndDecision | None:
    """Drives `tracker` while a recording is open: polls the RTP counters (if
    `sample_rtp` is given) and calls `on_end` once, when the caller has
    finished. Returns when the turn ended, or when `is_current()` turns false
    (recording finished by other means, or call over)."""
    config = tracker.config
    next_poll = clock()
    rtp_failures = 0
    while is_current():
        await asyncio.sleep(config.tick_seconds)
        now = clock()
        if sample_rtp is not None and config.use_rtp_statistics and rtp_failures < 3 and now >= next_poll:
            next_poll = now + config.rtp_poll_seconds
            counters = await sample_rtp()
            if counters is None:
                rtp_failures += 1
            else:
                rtp_failures = 0
                tracker.rtp_counters(clock(), *counters)
        if not is_current():
            break
        decision = tracker.decision(clock())
        if decision is not None:
            await on_end(decision)
            return decision
    return None
