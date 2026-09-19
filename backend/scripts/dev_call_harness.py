"""Real calls into a real Asterisk, with the real call controller, measured stage by stage.

Runs the production `AICallController` against Asterisk's real ARI and RTP
engine. The three external services (Bhashini ASR, Gemini, Bhashini TTS) are
replaced by stand-ins whose latency you choose, so what is being measured is
everything *else*: how the call is captured, when Asterisk decides the
caller stopped talking, how the controller reacts, what audio is actually
sent back over RTP, how quickly an interruption stops playback, and how long
the call stays up. The caller is `sim_gsm_caller.py`, which can behave like
the Synway in the client's packet capture (voice frames, then RTP payload
type 13 comfort noise during silence).

Run inside the WSL that hosts Asterisk, as a user allowed to read
/etc/asterisk/ari.conf and /var/spool/asterisk (root on a dev box):

    cd backend && /opt/sunway-ai-worker/venv/bin/python scripts/dev_call_harness.py \
        --scenario question --silence-mode cn

This measures the software path. It does NOT measure Bhashini/Gemini speed, the
Synway, the GSM network or a handset - those need a live call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sim_gsm_caller import SimGsmCaller, load_wav_mono  # noqa: E402

from app.core.config import Settings  # noqa: E402
from app.core.db import AsyncSessionLocal, engine  # noqa: E402
from app.models.ai import AIMessage, AISession  # noqa: E402
from app.models.calls import Call  # noqa: E402
from app.providers.embeddings.mock import MockEmbeddingProvider  # noqa: E402
from app.providers.llm.base import LLMProvider, LLMResponse  # noqa: E402
from app.providers.stt.base import STTProvider, TranscriptionResult  # noqa: E402
from app.providers.tts.base import SynthesisResult, TTSProvider  # noqa: E402
from app.services.ari_client import AriClient  # noqa: E402
from app.services.audio import read_wav_info, resample  # noqa: E402
from app.services.call_controller import AICallController  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "data" / "harness"
QUESTION_TEXT = "लम्पी रोग के लक्षण क्या हैं?"
REPLY_TEXT = (
    "लम्पी रोग में पशु को बुखार आता है और त्वचा पर गांठें बन जाती हैं। "
    "दूध कम हो जाता है और पशु खाना छोड़ देता है। "
    "ऐसे लक्षण दिखें तो पशु को अलग रखें और नजदीकी पशु चिकित्सक को दिखाएं।"
)


def _ari_password() -> str:
    text = Path("/etc/asterisk/ari.conf").read_text()
    section = re.search(r"^\[ai-agent\](.*?)(?=^\[|\Z)", text, re.S | re.M)
    match = re.search(r"^\s*password\s*=\s*(\S+)", section.group(1) if section else "", re.M)
    if not match:
        raise SystemExit("could not read the ai-agent ARI password from /etc/asterisk/ari.conf")
    return match.group(1)


class CallEnded(Exception):
    """Asterisk hung up (BYE from the AI side) while the test was waiting."""


class Timeline:
    """Every stage timestamp on one monotonic clock, from all components."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.events: list[tuple[float, str, dict]] = []
        self.log_lines: list[tuple[float, str]] = []

    def mark(self, _event: str, **detail) -> float:
        now = time.monotonic()
        self.events.append((now, _event, detail))
        return now

    def all(self, name: str) -> list[tuple[float, dict]]:
        return [(t, d) for t, n, d in self.events if n == name]

    def last(self, name: str) -> tuple[float, dict] | None:
        found = self.all(name)
        return found[-1] if found else None

    def rel(self, t: float) -> float:
        return round(t - self.t0, 3)


class _LogTap(logging.Handler):
    def __init__(self, timeline: Timeline) -> None:
        super().__init__(level=logging.INFO)
        self.timeline = timeline

    def emit(self, record: logging.LogRecord) -> None:
        self.timeline.log_lines.append((time.monotonic(), record.getMessage()))


class StandInSTT(STTProvider):
    def __init__(self, tl: Timeline, delay: float) -> None:
        self.tl, self.delay = tl, delay

    async def transcribe(self, audio_bytes: bytes, *, language: str | None = None) -> TranscriptionResult:
        info = read_wav_info(audio_bytes)
        self.tl.mark("asr_start", seconds=round(info.duration_seconds, 2), size=len(audio_bytes))
        await asyncio.sleep(self.delay)
        self.tl.mark("asr_end")
        return TranscriptionResult(text=QUESTION_TEXT, language=language, confidence=0.9, duration_seconds=info.duration_seconds)


class StandInLLM(LLMProvider):
    def __init__(self, tl: Timeline, delay: float) -> None:
        self.tl, self.delay = tl, delay

    @property
    def provider_name(self) -> str:
        return "StandIn"

    async def generate_response(self, *, system_prompt, history, retrieved_context) -> LLMResponse:
        self.tl.mark("llm_start")
        await asyncio.sleep(self.delay)
        self.tl.mark("llm_end")
        return LLMResponse(text=REPLY_TEXT, finish_reason="stop")


class StandInTTS(TTSProvider):
    def __init__(self, tl: Timeline, delay: float, wav: Path) -> None:
        self.tl, self.delay, self.wav = tl, delay, wav.read_bytes()

    async def synthesize(self, text: str, *, voice=None, language=None) -> SynthesisResult:
        self.tl.mark("tts_start", chars=len(text))
        await asyncio.sleep(self.delay)
        self.tl.mark("tts_end")
        return SynthesisResult(audio_bytes=self.wav, audio_format="wav")


class LoopLag:
    """Records every moment this process's event loop (which also runs the simulated
    gateway) was blocked for more than 50 ms. A measurement taken while the machine was
    starved is not evidence about the worker, and this makes such a run identifiable."""

    def __init__(self) -> None:
        self.stalls: list[tuple[float, float]] = []  # (monotonic time, lateness ms)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        interval = 0.01
        expected = time.monotonic() + interval
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            late = (now - expected) * 1000
            if late > 50:
                self.stalls.append((now, late))
            expected = time.monotonic() + interval

    def worst(self, since: float, until: float) -> float:
        return max((late for t, late in self.stalls if since <= t <= until), default=0.0)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)


class Harness:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tl = Timeline()
        overrides = dict(
            _env_file=None,
            app_env="development",
            ai_language="hi",
            ai_persona="",
            llm_provider="standin",
            rag_embedding_provider="mock",
            rag_similarity_threshold=-1.0,
            asterisk_recording_spool_path="/var/spool/asterisk/recording",
            ai_end_of_speech_silence_seconds=args.end_silence,
            ai_max_turn_seconds=args.max_turn,
            ai_call_timeout_seconds=args.call_timeout,
            ai_turn_timeout_seconds=60.0,
            provider_timeout_seconds=30.0,
            ai_no_input_timeout_seconds=90,
        )
        for item in args.set or []:
            key, _, value = item.partition("=")
            overrides[key] = json.loads(value) if value[:1] in "0123456789-[{\"" or value in ("true", "false") else value
        self.settings = Settings(**overrides)
        self.ari = AriClient(
            base_url=self.settings.resolved_ari_url(),
            username=self.settings.asterisk_ari_username or "ai-agent",
            password=_ari_password(),
            app=self.settings.asterisk_ari_app,
        )
        self._wrap_ari()
        self.controller = AICallController(
            ari=self.ari,
            settings=self.settings,
            session_factory=AsyncSessionLocal,
            embedding_provider=MockEmbeddingProvider(dimensions=1536),
            llm_provider=StandInLLM(self.tl, args.llm_delay),
            stt_provider=StandInSTT(self.tl, args.asr_delay),
            tts_provider=StandInTTS(self.tl, args.tts_delay, FIXTURES / "tts_zira_short_48k.wav"),
        )
        self._wrap_controller()
        self.run_task: asyncio.Task | None = None
        self.channel_ids: set[str] = set()
        self.lag = LoopLag()
        self.loadavg_start = os.getloadavg()[0] if hasattr(os, "getloadavg") else None

    def _wrap_ari(self) -> None:
        ari, tl = self.ari, self.tl
        record, play, stop, hangup = ari.record, ari.play, ari.stop_playback, ari.hangup

        async def rec(channel_id, **kw):
            tl.mark("record_start", name=kw.get("name"), max_silence=kw.get("max_silence_seconds"), max_duration=kw.get("max_duration_seconds"))
            self.channel_ids.add(channel_id)
            return await record(channel_id, **kw)

        async def pl(channel_id, **kw):
            tl.mark("play_request", media=Path(kw.get("media", "")).name)
            return await play(channel_id, **kw)

        async def st(playback_id):
            tl.mark("stop_playback_request")
            return await stop(playback_id)

        async def hu(channel_id, **kw):
            tl.mark("worker_hangup", reason=kw.get("reason"))
            return await hangup(channel_id, **kw)

        ari.record, ari.play, ari.stop_playback, ari.hangup = rec, pl, st, hu

    def _wrap_controller(self) -> None:
        controller, tl = self.controller, self.tl
        finished = controller._on_recording_finished
        talking = controller._on_talking_started

        async def rf(event):
            r = event.get("recording", {})
            tl.mark("recording_finished", name=r.get("name"), duration=r.get("duration"), talking=r.get("talking_duration"), silence=r.get("silence_duration"))
            return await finished(event)

        async def tk(event):
            tl.mark("talking_started_event")
            return await talking(event)

        controller._on_recording_finished = rf
        controller._on_talking_started = tk

    async def start(self) -> None:
        logging.getLogger("app").addHandler(_LogTap(self.tl))
        logging.getLogger("app").setLevel(logging.INFO)
        await self.controller.prewarm_caller_messages()
        self.lag.start()
        self.run_task = asyncio.create_task(self.controller.run_forever())
        for _ in range(100):  # wait until the Stasis app is registered
            try:
                response = await self.ari._http.get(f"/applications/{self.settings.asterisk_ari_app}")
                if response.status_code == 200:
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.1)
        raise SystemExit("ARI application did not register")

    async def stop(self) -> None:
        await self.lag.stop()
        if self.run_task:
            self.run_task.cancel()
            await asyncio.gather(self.run_task, return_exceptions=True)
        await self.ari.aclose()
        from sqlalchemy import delete, select

        async with AsyncSessionLocal() as db:
            calls = (await db.execute(select(Call).where(Call.asterisk_channel_id.in_(self.channel_ids)))).scalars().all()
            for call in calls:
                sessions = (await db.execute(select(AISession).where(AISession.call_id == call.id))).scalars().all()
                for s in sessions:
                    await db.execute(delete(AIMessage).where(AIMessage.session_id == s.id))
                    await db.delete(s)
                await db.delete(call)
            await db.commit()
        await engine.dispose()

    # ------------------------------------------------------------ helpers
    def new_caller(self) -> SimGsmCaller:
        return SimGsmCaller(silence_mode=self.args.silence_mode, cn_interval_ms=self.args.cn_interval_ms)

    async def wait_until(self, predicate, timeout: float, what: str):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.01)
        raise TimeoutError(f"timed out after {timeout}s waiting for {what}")

    def caller_pcm8k(self, name: str = "caller_david_48k.wav") -> np.ndarray:
        samples, rate = load_wav_mono(FIXTURES / name)
        return np.clip(np.round(resample(samples.astype(np.float64) / 32768.0, rate, 8000) * 32767), -32768, 32767).astype(np.int16)

    def parse_turn_timing(self) -> list[dict]:
        rows = []
        for _, line in self.tl.log_lines:
            if line.startswith("Turn timing"):
                rows.append({k: v for k, v in re.findall(r"(\w+)=([\-\d\.]+)(?:ms|s|x|dBFS)?", line)})
        return rows

    def controller_settings(self) -> dict:
        s = self.settings
        return {k: getattr(s, k) for k in ("ai_end_of_speech_silence_seconds", "ai_max_turn_seconds", "ai_call_timeout_seconds", "ai_tts_speed")}


async def turn(h: Harness, caller: SimGsmCaller, *, label: str, speak: bool = True) -> dict:
    """One caller question -> the AI reply, measured end to end."""
    tl = h.tl
    n_records = len(tl.all("record_start"))

    def alive_or_raise() -> None:
        if caller.remote_hangup_at is not None:
            raise CallEnded()

    def until(predicate):
        def check():
            alive_or_raise()
            return predicate()
        return check

    # Speak into the recording that is already open (the AI starts listening the
    # moment the previous prompt ends); only wait for a new one if none is open.
    if len(tl.all("record_start")) <= len(tl.all("recording_finished")):
        await h.wait_until(until(lambda: len(tl.all("record_start")) > len(tl.all("recording_finished"))), 40, "recording to start")
    record_start = tl.last("record_start")[0]
    await asyncio.sleep(h.args.think_time)
    pcm = h.caller_pcm8k()
    # "Caller stopped talking" = the last frame that is actually speech, not the
    # end of the (quiet-tailed) audio file: that is what a person means by it.
    mags = np.abs(pcm[: len(pcm) // 160 * 160].astype(np.float64)).reshape(-1, 160).mean(axis=1)
    voiced = np.nonzero(mags > 200)[0]
    last_speech_offset = (voiced[-1] + 1) * 0.02 if len(voiced) else len(pcm) / 8000
    expected_speech_frames = len(voiced)
    if h.args.tail_ms is not None:
        # a gateway whose VAD stops sending voice frames `tail_ms` after the last speech frame
        pcm = pcm[: int(min(len(pcm) / 8000, last_speech_offset + h.args.tail_ms / 1000) * 8000)]
    n_finished, n_asr = len(tl.all("recording_finished")), len(tl.all("asr_start"))
    n_sent = len(caller.speech_sent_at)
    speech_start = tl.mark("caller_speech_start")
    await caller.speak(pcm, wait=True)
    tl.mark("caller_speech_end")
    sent = caller.speech_sent_at[n_sent:]
    # the moment the frame carrying the last speech was actually put on the wire
    last_frame = min(len(sent) - 1, max(0, round(last_speech_offset / 0.02) - 1))
    speech_end = sent[last_frame] if sent else speech_start + last_speech_offset
    send_lag_ms = round(((sent[-1] - sent[0]) - (len(sent) - 1) * 0.02) * 1000) if len(sent) > 1 else 0
    reply_first = await h.wait_until(
        until(lambda: next((p.t for p in caller.rx if p.payload_type == 0 and p.t > speech_end), None)),
        60, "AI reply audio",
    )
    finished = tl.all("recording_finished")
    rec_done = finished[n_finished][0] if len(finished) > n_finished else None
    rec_detail = finished[n_finished][1] if rec_done else {}

    def first(name: str, after: float) -> float | None:
        return next((t for t, _ in tl.all(name) if t >= after), None)

    asr_s, asr_e = first("asr_start", speech_start), first("asr_end", speech_start)
    llm_s, llm_e = first("llm_start", speech_start), first("llm_end", speech_start)
    tts_s, tts_e = first("tts_start", asr_e or speech_start), first("tts_end", asr_e or speech_start)
    play_req = first("play_request", (llm_e or speech_start))

    def ms(a, b):
        return None if a is None or b is None else round((b - a) * 1000)

    capture_wall = ms(record_start, rec_done)
    captured_pct = None
    if rec_done and rec_detail.get("name") and expected_speech_frames:
        try:
            raw = (Path(h.settings.asterisk_recording_spool_path) / f"{rec_detail['name']}.wav").read_bytes()[44:]
            usable = len(raw) // 320
            recorded = np.abs(np.frombuffer(raw[: usable * 320], dtype="<i2").astype(np.float64)).reshape(-1, 160).mean(axis=1)
            captured_pct = round(100.0 * int((recorded > 200).sum()) / expected_speech_frames, 1)
        except OSError:
            captured_pct = None
    return {
        "turn": label,
        "sim_send_lag_ms (simulator's own pacing error while sending the question)": send_lag_ms,
        "event_loop_stall_max_ms (record start -> reply heard; >100 = machine was starved, run is suspect)":
            round(h.lag.worst(record_start, reply_first)),
        "speech_captured_pct (recorded WAV vs what the caller said; <100 = caller cut off)": captured_pct,
        "caller_speech_s (first to last speech frame)": round(last_speech_offset, 2),
        "capture_ms (record start -> recording finished)": capture_wall,
        "end_of_speech_wait_ms (last speech -> recording finished)": ms(speech_end, rec_done),
        "ended_by": "MAX DURATION (silence never detected)" if (capture_wall or 0) >= h.args.max_turn * 1000 - 800 else "silence detected",
        "read_and_prepare_ms (recording finished -> ASR start)": ms(rec_done, asr_s),
        "asr_ms (stand-in)": ms(asr_s, asr_e),
        "retrieval_ms (ASR end -> LLM start)": ms(asr_e, llm_s),
        "llm_ms (stand-in)": ms(llm_s, llm_e),
        "tts_first_chunk_ms (stand-in)": ms(tts_s, tts_e),
        "tts_end_to_play_request_ms": ms(tts_e, play_req),
        "play_request_to_first_rtp_ms": ms(play_req, reply_first),
        "RESPONSE_HEARD_ms (last caller speech -> first reply RTP)": ms(speech_end, reply_first),
        "_reply_first_t": reply_first,
    }


async def scenario_question(h: Harness) -> dict:
    caller = h.new_caller()
    await caller.start()
    await caller.invite()
    answered = caller.first_event("200_ok").t
    welcome_start = await h.wait_until(lambda: next((p.t for p in caller.rx if p.payload_type == 0), None), 15, "welcome audio")
    await h.wait_until(lambda: time.monotonic() - caller.rx[-1].t > 0.6 if caller.rx else False, 30, "welcome to end")
    welcome_end = caller.rx[-1].t
    turns = []
    for i in range(h.args.turns):
        result = await turn(h, caller, label=f"question {i + 1}")
        # let the reply finish before the next question, like a real caller
        reply_t = result.pop("_reply_first_t")
        await h.wait_until(lambda: time.monotonic() - caller.rx[-1].t > 0.8, 60, "reply to finish")
        result["reply_duration_s"] = round(caller.rx[-1].t - reply_t, 2)
        bursts = [b for b in caller.audio_bursts() if b["start"] >= reply_t - 0.05]
        result["reply_bursts (playbacks heard)"] = len(bursts)
        result["gaps_between_reply_chunks_ms"] = [round((b2["start"] - b1["end"]) * 1000) for b1, b2 in zip(bursts, bursts[1:])]
        turns.append(result)
    await caller.bye()
    out = {
        "answer_to_welcome_first_rtp_ms": round((welcome_start - answered) * 1000),
        "welcome_duration_s": round(welcome_end - welcome_start, 2),
        "rtp_negotiated": caller.negotiated_payloads,
        "sim_tx_by_payload_type": caller.tx_by_type,
        "turns": turns,
    }
    await caller.close()
    return out


async def scenario_bargein(h: Harness) -> dict:
    caller = h.new_caller()
    await caller.start()
    await caller.invite()
    await h.wait_until(lambda: next((p for p in caller.rx if p.payload_type == 0), None), 15, "welcome audio")
    await h.wait_until(lambda: time.monotonic() - caller.rx[-1].t > 0.6 if caller.rx else False, 30, "welcome to end")
    first = await turn(h, caller, label="question (to start a reply)")
    reply_first = first.pop("_reply_first_t")
    # the reply is under way: interrupt it `interrupt_after` seconds after its first RTP packet
    await asyncio.sleep(max(0.0, reply_first + h.args.interrupt_after - time.monotonic()))
    ai_playing_at_interrupt = bool(caller.rx) and time.monotonic() - caller.rx[-1].t < 0.1
    n_rx = len(caller.rx)
    n_stop = len(h.tl.all("stop_playback_request"))
    talk_first = h.tl.mark("interrupt_speech_start")
    speaking = asyncio.create_task(caller.speak(h.caller_pcm8k("caller_david_48k.wav"), wait=True))
    stop_req = None
    try:
        await h.wait_until(lambda: len(h.tl.all("stop_playback_request")) > n_stop, 3, "stop_playback request")
        stop_req = h.tl.last("stop_playback_request")[0]
    except TimeoutError:
        pass
    talk_evt = next((t for t, _ in h.tl.all("talking_started_event") if t >= talk_first), None)
    await speaking
    speech_end = time.monotonic()
    # AI audio that kept arriving while the caller was talking (after a 0.5 s grace) = the AI talked over them
    overlap = [p for p in caller.rx[n_rx:] if p.payload_type == 0 and talk_first + 0.5 < p.t <= speech_end]
    last_ai = max((p.t for p in caller.rx[n_rx:] if p.payload_type == 0), default=None)
    await asyncio.sleep(1.5)
    n_after = len(h.tl.all("record_start"))
    await caller.bye()
    out = {
        "AI_was_audibly_speaking_at_the_moment_of_interruption": ai_playing_at_interrupt,
        "first_question": first,
        "interruption": {
            "caller_started_speaking -> TALK_DETECT event (ms)": None if talk_evt is None else round((talk_evt - talk_first) * 1000),
            "caller_started_speaking -> stop_playback request (ms)": None if stop_req is None else round((stop_req - talk_first) * 1000),
            "caller_started_speaking -> last AI RTP packet (ms)": None if last_ai is None else round((last_ai - talk_first) * 1000),
            "playback_was_stopped_by_the_interruption": stop_req is not None,
            "AI audio heard WHILE the caller was speaking (seconds)": round(len(overlap) * 0.02, 2),
        },
    }
    await caller.close()
    return out


async def scenario_longcall(h: Harness) -> dict:
    caller = h.new_caller()
    await caller.start()
    await caller.invite()
    started = caller.first_event("200_ok").t
    await h.wait_until(lambda: next((p for p in caller.rx if p.payload_type == 0), None), 15, "welcome audio")
    await h.wait_until(lambda: time.monotonic() - caller.rx[-1].t > 0.6 if caller.rx else False, 30, "welcome to end")
    turns = []
    end_reason = "planned duration reached"
    while time.monotonic() - started < h.args.duration:
        if caller.remote_hangup_at is not None:
            end_reason = "Asterisk hung up (BYE from the AI side)"
            break
        try:
            result = await turn(h, caller, label=f"question {len(turns) + 1}")
        except CallEnded:
            end_reason = "Asterisk hung up (BYE from the AI side)"
            break
        except TimeoutError:
            end_reason = "no reply / call ended while waiting"
            break
        reply_t = result.pop("_reply_first_t")
        turns.append({"at_s": round(time.monotonic() - started, 1), "heard_ms": result["RESPONSE_HEARD_ms (last caller speech -> first reply RTP)"]})
        try:
            await h.wait_until(lambda: time.monotonic() - caller.rx[-1].t > 0.8, 60, "reply to finish")
        except TimeoutError:
            break
        # an occasional caller pause, as in a real conversation
        await asyncio.sleep(h.args.gap)
    if caller.remote_hangup_at is not None:
        end_reason = "Asterisk hung up (BYE from the AI side)"
    duration = (caller.remote_hangup_at or time.monotonic()) - started
    worker_hangup = h.tl.last("worker_hangup")
    timeout_logged = any("Call timeout reached" in line for _, line in h.tl.log_lines)
    if caller.remote_hangup_at is None:
        await caller.bye()
    out = {
        "call_duration_s": round(duration, 1),
        "ended_because": end_reason,
        "worker_logged_call_timeout": timeout_logged,
        "worker_hangup_after_s": None if worker_hangup is None else round(worker_hangup[0] - started, 1),
        "answers_received": len(turns),
        "per_turn_heard_ms": turns,
    }
    await caller.close()
    return out


SCENARIOS = {"question": scenario_question, "bargein": scenario_bargein, "longcall": scenario_longcall}


async def main_async(args: argparse.Namespace) -> int:
    harness = Harness(args)
    await harness.start()
    try:
        result = await SCENARIOS[args.scenario](harness)
    finally:
        await harness.stop()
    report = {
        "scenario": args.scenario,
        "silence_mode": args.silence_mode,
        "settings": harness.controller_settings(),
        "stand_in_latency_s": {"asr": args.asr_delay, "llm": args.llm_delay, "tts_per_chunk": args.tts_delay},
        "host": {
            "loadavg_1m_at_start": None if harness.loadavg_start is None else round(harness.loadavg_start, 2),
            "loadavg_1m_at_end": None if not hasattr(os, "getloadavg") else round(os.getloadavg()[0], 2),
            "cpu_count": os.cpu_count(),
            "event_loop_stalls_over_50ms": len(harness.lag.stalls),
            "worst_event_loop_stall_ms": round(max((late for _, late in harness.lag.stalls), default=0.0)),
        },
        "result": result,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--scenario", choices=list(SCENARIOS), default="question")
    p.add_argument("--silence-mode", choices=["frames", "cn", "dtx"], default="frames")
    p.add_argument("--cn-interval-ms", type=int, default=200)
    p.add_argument("--asr-delay", type=float, default=1.2)
    p.add_argument("--llm-delay", type=float, default=1.0)
    p.add_argument("--tts-delay", type=float, default=1.0)
    p.add_argument("--end-silence", type=int, default=2, help="AI_END_OF_SPEECH_SILENCE_SECONDS")
    p.add_argument("--max-turn", type=int, default=20, help="AI_MAX_TURN_SECONDS")
    p.add_argument("--call-timeout", type=int, default=120, help="AI_CALL_TIMEOUT_SECONDS")
    p.add_argument("--turns", type=int, default=1)
    p.add_argument("--think-time", type=float, default=0.5, help="caller pause after the prompt before speaking")
    p.add_argument("--tail-ms", type=int, default=None, help="voice frames the gateway keeps sending after the last speech frame (default: the clip's own 780 ms tail); 0 = none")
    p.add_argument("--interrupt-after", type=float, default=1.2)
    p.add_argument("--duration", type=float, default=200.0)
    p.add_argument("--gap", type=float, default=1.0)
    p.add_argument("--set", action="append", help="extra Settings override, e.g. --set ai_tts_speed=1.0")
    p.add_argument("--out")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
