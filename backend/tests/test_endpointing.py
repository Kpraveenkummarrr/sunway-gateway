"""End-of-speech detection independent of the gateway's RTP behaviour.

Two regressions are guarded here.

1. With a gateway that stops sending voice frames during silence (RTP comfort
   noise / VAD), Asterisk's own silence detector never fires and every question
   waited for the 20 s recording cap. The tracker must end the turn from
   Asterisk's talk-detect events or from the RTP counters instead.

2. An earlier version followed the recording FILE. Asterisk writes that file in
   32 KiB blocks (2 s of audio), so "the file has not grown for 1.2 s" cut callers
   off mid-sentence (measured on a live call). Nothing here reads the file; the
   controller-level test proves a stalled file cannot end a turn.
"""

import asyncio
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from app.services.call_controller import CallState
from app.services.endpointing import (
    REASON_NO_VOICE,
    REASON_TALK_DETECT,
    EndOfSpeechTracker,
    EndpointConfig,
    monitor_turn,
)
from tests.fake_ari import talking_finished_event, talking_started_event
from tests.test_call_controller import _make_controller

CFG = EndpointConfig(silence_ms=1200, min_speech_ms=250, talk_detect_silence_ms=200)
VOICE = (10, 1600)  # per 200 ms: 10 PCMU packets of 160 bytes
COMFORT = (1, 1)  # one 1-byte RFC 3389 packet
NOTHING = (0, 0)  # DTX: no packets at all


class Feed:
    """Cumulative RTP counters advanced in 200 ms steps."""

    def __init__(self, tracker: EndOfSpeechTracker, start: float) -> None:
        self.tracker, self.now, self.packets, self.octets = tracker, start, 1000, 160000
        tracker.rtp_counters(start, self.packets, self.octets)

    def step(self, kind: tuple[int, int], count: int = 1) -> float:
        for _ in range(count):
            self.now += 0.2
            self.packets += kind[0]
            self.octets += kind[1]
            self.tracker.rtp_counters(self.now, self.packets, self.octets)
        return self.now


# ---- TALK_DETECT events ----


def test_the_turn_ends_a_fixed_wait_after_talk_detect_reports_finished() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    tracker.talking_started(1.0)
    tracker.talking_finished(3.4, duration_ms=2400)  # Asterisk reports it 200 ms after the last speech

    assert tracker.decision(3.4) is None
    assert tracker.decision(4.3) is None  # 0.9 s after the event: 1.1 s of silence in all
    decision = tracker.decision(4.5)
    assert decision is not None and decision.reason == REASON_TALK_DETECT
    assert decision.last_speech_at == pytest.approx(3.2)  # the event's 200 ms latency is taken off
    assert decision.speech_ms == 2400


def test_speech_resuming_within_the_wait_keeps_the_turn_open() -> None:
    """A pause inside a sentence: the next 'started' cancels the pending end."""
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    tracker.talking_started(1.0)
    tracker.talking_finished(2.2, duration_ms=1200)
    tracker.talking_started(3.0)  # spoke again 0.8 s later
    assert tracker.decision(5.0) is None  # still talking, however long it takes
    tracker.talking_finished(5.4, duration_ms=2400)
    assert tracker.decision(5.6) is None
    assert tracker.decision(6.5) is not None


def test_a_click_or_burst_is_not_a_turn() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    tracker.talking_started(1.0)
    tracker.talking_finished(1.3, duration_ms=100)
    assert tracker.decision(20.0) is None


def test_a_turn_with_no_speech_never_ends_by_itself() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    assert tracker.decision(0.5) is None
    assert tracker.decision(60.0) is None


def test_a_caller_who_is_still_talking_is_never_cut_off() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    tracker.talking_started(1.0)
    assert tracker.decision(9.0) is None  # no 'finished' yet: Asterisk still hears speech


def test_a_caller_who_interrupted_the_prompt_is_already_talking_when_the_recording_opens() -> None:
    tracker = EndOfSpeechTracker(CFG, now=10.0, already_talking=True)
    tracker.talking_finished(12.0, duration_ms=1800)
    assert tracker.decision(12.5) is None
    assert tracker.decision(13.1) is not None


def test_a_missing_duration_falls_back_to_the_time_since_started() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    tracker.talking_started(1.0)
    tracker.talking_finished(3.4, duration_ms=None)
    assert tracker.speech_ms >= 2000
    assert tracker.decision(4.5) is not None


# ---- RTP receive counters: gateways that stop sending voice frames ----


def test_a_comfort_noise_gateway_ends_the_turn_when_voice_frames_stop() -> None:
    """The client's case: PT 13 comfort noise after the last word, no TALK_DETECT 'finished'."""
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    feed = Feed(tracker, start=0.0)
    feed.step(VOICE, 5)  # 1.0 s of speech
    last_voice = feed.step(VOICE, 5)  # 2.0 s
    feed.step(COMFORT, 3)  # 0.6 s of comfort noise
    assert tracker.decision(feed.now) is None
    feed.step(COMFORT, 3)  # 1.2 s
    decision = tracker.decision(feed.now + 0.05)
    assert decision is not None and decision.reason == REASON_NO_VOICE
    assert decision.last_speech_at == pytest.approx(last_voice)


def test_a_silent_dtx_gateway_ends_the_turn_the_same_way() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    feed = Feed(tracker, start=0.0)
    feed.step(VOICE, 8)
    feed.step(NOTHING, 5)
    assert tracker.decision(feed.now) is None  # 1.0 s
    feed.step(NOTHING, 2)
    assert tracker.decision(feed.now) is not None  # 1.4 s


def test_a_gateway_that_keeps_sending_frames_is_never_ended_by_the_rtp_signal() -> None:
    """Continuous frames (noise between words too): that case belongs to TALK_DETECT."""
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    feed = Feed(tracker, start=0.0)
    for _ in range(100):  # 20 s of frames, sampled every 200 ms like the monitor does
        feed.step(VOICE)
        assert tracker.decision(feed.now + 0.05) is None


def test_rtp_evidence_needs_real_speech_not_a_burst() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    feed = Feed(tracker, start=0.0)
    feed.step(COMFORT, 3)
    feed.step(VOICE, 1)  # one 200 ms voice burst < 250 ms of speech... plus the sampling slack
    feed.step(COMFORT, 10)
    burst_only = tracker.decision(feed.now)
    tracker2 = EndOfSpeechTracker(CFG, now=0.0)
    feed2 = Feed(tracker2, start=0.0)
    feed2.step(COMFORT, 3)
    feed2.step(VOICE, 3)  # 600 ms
    feed2.step(COMFORT, 10)
    assert burst_only is None
    assert tracker2.decision(feed2.now) is not None


def test_voice_in_the_first_moments_is_the_prompt_echoing_back() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    feed = Feed(tracker, start=0.0)
    # a sample taken within the first 200 ms that shows voice-sized packets
    feed.packets += 10
    feed.octets += 1600
    tracker.rtp_counters(0.1, feed.packets, feed.octets)
    feed.now = 0.1
    feed.step(COMFORT, 20)
    assert tracker.decision(feed.now) is None


def test_the_rtp_signal_can_be_switched_off() -> None:
    tracker = EndOfSpeechTracker(EndpointConfig(use_rtp_statistics=False), now=0.0)
    feed = Feed(tracker, start=0.0)
    feed.step(VOICE, 10)
    feed.step(NOTHING, 20)
    assert tracker.decision(feed.now) is None


def test_ten_millisecond_packets_still_count_as_voice() -> None:
    """ptime 10 ms: 80-byte packets - still far above a 1-13 byte comfort-noise packet."""
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    feed = Feed(tracker, start=0.0)
    feed.step((20, 1600), 6)
    feed.step(COMFORT, 7)
    assert tracker.decision(feed.now) is not None


def test_a_window_that_is_mostly_voice_with_one_comfort_packet_counts_as_voice() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    feed = Feed(tracker, start=0.0)
    feed.step((10, 9 * 160 + 1), 10)  # 9 voice + 1 comfort per window
    assert tracker.decision(feed.now + 0.1) is None


def test_either_signal_ends_the_turn_whichever_comes_first() -> None:
    tracker = EndOfSpeechTracker(CFG, now=0.0)
    tracker.talking_started(0.4)
    feed = Feed(tracker, start=0.0)
    feed.step(VOICE, 8)
    feed.step(COMFORT, 6)  # 1.2 s: the RTP signal is ready first ...
    tracker.talking_finished(feed.now + 0.5, duration_ms=1900)  # ... 'finished' arrives (late) afterwards
    decision = tracker.decision(feed.now + 0.1)
    assert decision is not None and decision.reason == REASON_NO_VOICE


# ---- the async monitor ----


@pytest.mark.asyncio
async def test_the_monitor_ends_a_comfort_noise_turn_from_rtp_counters() -> None:
    config = EndpointConfig(silence_ms=300, min_speech_ms=100, start_grace_ms=0, tick_seconds=0.01, rtp_poll_seconds=0.03)
    tracker = EndOfSpeechTracker(config, now=time.monotonic())
    started = time.monotonic()
    ended: list[tuple[float, str]] = []

    async def sample() -> tuple[int, int] | None:
        elapsed = time.monotonic() - started
        packets = int(min(elapsed, 0.3) / 0.02) + int(max(elapsed - 0.3, 0) / 0.2)  # voice for 0.3 s, then comfort noise
        octets = int(min(elapsed, 0.3) / 0.02) * 160 + int(max(elapsed - 0.3, 0) / 0.2)
        return packets, octets

    async def on_end(decision) -> None:
        ended.append((time.monotonic() - started, decision.reason))

    await asyncio.wait_for(
        monitor_turn(tracker, is_current=lambda: not ended, on_end=on_end, sample_rtp=sample), timeout=3
    )
    assert ended, "the turn was never ended"
    elapsed, reason = ended[0]
    assert reason == REASON_NO_VOICE
    assert 0.55 <= elapsed <= 1.0, f"ended {elapsed:.2f}s in (voice stopped at 0.3 s, wait 0.3 s)"


@pytest.mark.asyncio
async def test_the_monitor_keeps_working_from_talk_detect_when_rtp_statistics_are_unavailable() -> None:
    config = EndpointConfig(silence_ms=400, min_speech_ms=100, talk_detect_silence_ms=100, tick_seconds=0.01, rtp_poll_seconds=0.02)
    tracker = EndOfSpeechTracker(config, now=time.monotonic())
    calls = 0
    ended: list[str] = []

    async def sample() -> None:
        nonlocal calls
        calls += 1
        return None

    async def on_end(decision) -> None:
        ended.append(decision.reason)

    task = asyncio.create_task(
        monitor_turn(tracker, is_current=lambda: not ended, on_end=on_end, sample_rtp=sample)
    )
    tracker.talking_started(time.monotonic())
    await asyncio.sleep(0.15)
    tracker.talking_finished(time.monotonic(), duration_ms=300)
    await asyncio.wait_for(task, timeout=2)
    assert ended == [REASON_TALK_DETECT]
    assert calls == 3, "an unavailable statistics endpoint must be given up on, not polled forever"


@pytest.mark.asyncio
async def test_the_monitor_stops_quietly_when_the_turn_is_no_longer_current() -> None:
    tracker = EndOfSpeechTracker(EndpointConfig(tick_seconds=0.01), now=time.monotonic())
    current = {"value": True}

    async def on_end(decision) -> None:
        raise AssertionError("must not end a turn that never had speech")

    task = asyncio.create_task(monitor_turn(tracker, is_current=lambda: current["value"], on_end=on_end))
    await asyncio.sleep(0.1)
    current["value"] = False
    assert await asyncio.wait_for(task, timeout=2) is None


# ---- controller wiring ----


async def _controller_with_open_recording(tmp: str, **overrides):
    controller, ari = _make_controller(
        Path(tmp), ai_endpoint_silence_ms=400, ai_endpoint_min_speech_ms=100, ai_talk_detect_silence_ms=100, **overrides
    )
    state = CallState("PJSIP/700-EP", uuid.uuid4(), uuid.uuid4())
    controller._calls[state.channel_id] = state
    await controller._start_next_recording(state)
    return controller, ari, state


async def _close(state: CallState) -> None:
    """Ends the call state and lets the monitor task notice, so no task outlives the test."""
    state.ended.set()
    if state.recording_task is not None:
        await asyncio.wait_for(asyncio.gather(state.recording_task, return_exceptions=True), timeout=2)


async def _until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.mark.asyncio
async def test_the_controller_ends_the_recording_after_talk_detect_finishes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, state = await _controller_with_open_recording(tmp)
        await controller.dispatch_event(talking_started_event(state.channel_id))
        await asyncio.sleep(0.05)
        await controller.dispatch_event(talking_finished_event(state.channel_id, duration_ms=1500))
        assert await _until(lambda: bool(ari.stopped_recordings)), "the worker never ended the turn"
        assert ari.stopped_recordings == [state.recording_name]
        assert state.endpoint_reason == REASON_TALK_DETECT
        await _close(state)


@pytest.mark.asyncio
async def test_a_recording_file_that_has_not_grown_never_ends_a_turn() -> None:
    """The measured defect: Asterisk writes the file in 2 s blocks, so a file that
    stays at its 44-byte header while the caller is talking is normal. Voice is
    still arriving (RTP counters growing) and no 'finished' has been reported: the
    turn must stay open, however long the file is silent."""
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, state = await _controller_with_open_recording(tmp)
        (Path(tmp) / f"{state.recording_name}.wav").write_bytes(b"RIFF" + bytes(40))  # header only
        await controller.dispatch_event(talking_started_event(state.channel_id))
        for step in range(1, 26):  # 2.5 s > the 0.4 s wait, with voice frames flowing
            ari.rtp_statistics["rxcount"] = 100 + step * 10
            ari.rtp_statistics["rxoctetcount"] = 16000 + step * 1600
            await asyncio.sleep(0.1)
        assert ari.stopped_recordings == [], "a stalled recording file must not end a turn"
        await _close(state)


@pytest.mark.asyncio
async def test_the_controller_ends_a_comfort_noise_turn_from_the_rtp_counters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, state = await _controller_with_open_recording(tmp)
        await controller.dispatch_event(talking_started_event(state.channel_id))  # no 'finished' will ever come
        for step in range(1, 9):  # 0.8 s of voice packets
            ari.rtp_statistics["rxcount"] = 100 + step * 10
            ari.rtp_statistics["rxoctetcount"] = 16000 + step * 1600
            await asyncio.sleep(0.1)
        # the gateway now sends only comfort noise
        assert await _until(lambda: bool(ari.stopped_recordings), timeout=3.0), "comfort noise pinned the turn open"
        assert state.endpoint_reason == REASON_NO_VOICE
        await _close(state)


@pytest.mark.asyncio
async def test_talk_detect_is_reset_at_the_end_of_every_turn_so_barge_in_stays_armed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, state = await _controller_with_open_recording(tmp)
        settings_before = len(ari.talk_detect_settings)
        from tests.fake_ari import recording_finished_event

        (Path(tmp) / f"{state.recording_name}.wav").write_bytes(b"")
        await controller.dispatch_event(recording_finished_event(state.recording_name))
        assert await _until(lambda: state.channel_id in ari.talk_detect_removals)
        assert await _until(lambda: len(ari.talk_detect_settings) > settings_before), "detector removed but not re-armed"
        assert state.talking is False
        await _close(state)


@pytest.mark.asyncio
async def test_talk_detect_is_left_alone_when_the_dialplan_owns_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, state = await _controller_with_open_recording(tmp, ai_talk_detect_override=False)
        from tests.fake_ari import recording_finished_event

        (Path(tmp) / f"{state.recording_name}.wav").write_bytes(b"")
        await controller.dispatch_event(recording_finished_event(state.recording_name))
        await asyncio.sleep(0.1)
        assert ari.talk_detect_removals == []  # never remove what the dialplan set up
        await _close(state)


@pytest.mark.asyncio
async def test_a_caller_already_talking_when_the_recording_opens_is_tracked_from_the_start() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari = _make_controller(Path(tmp), ai_endpoint_silence_ms=400, ai_endpoint_min_speech_ms=100,
                                           ai_talk_detect_silence_ms=100)
        state = CallState("PJSIP/700-EP2", uuid.uuid4(), uuid.uuid4())
        controller._calls[state.channel_id] = state
        await controller.dispatch_event(talking_started_event(state.channel_id))  # before any recording is open
        assert state.talking is True
        await controller._start_next_recording(state)
        await controller.dispatch_event(talking_finished_event(state.channel_id, duration_ms=1500))
        assert await _until(lambda: bool(ari.stopped_recordings))
        await _close(state)


@pytest.mark.asyncio
async def test_the_monitor_can_be_switched_off() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        controller, ari, state = await _controller_with_open_recording(tmp, ai_endpoint_monitor=False)
        assert state.tracker is None and state.recording_task is None
        await _close(state)
