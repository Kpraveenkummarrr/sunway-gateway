"""Regression coverage for defects isolated in the final engineering pass."""
import asyncio
import base64
import json
import time
import uuid

import pytest
from fastapi import HTTPException

from app.core.admin_auth import _sign, issue_session, password_matches, session_is_valid
from app.core.config import Settings
from app.core.security import require_internal_api_key
from app.services.call_controller import CallState
from app.services.knowledge_search import build_retrieval_query
from app.services.pdf_extraction import normalize_text
from app.services.system_config import ConfigError, coerce
from tests.test_call_controller import _make_controller
from tests.fake_ari import recording_finished_event


def test_password_only_admin_rejects_public_fallback_cookie():
    settings = Settings(_env_file=None, admin_password="private-test-password", internal_api_key="", app_secret_key="")
    payload = json.dumps({"exp": time.time() + 1000}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    assert not session_is_valid(f"{encoded}.{_sign(payload, 'sunway-dev-unsigned')}", settings)
    assert session_is_valid(issue_session(settings), settings)


@pytest.mark.parametrize("body", [[], {"exp": "tomorrow"}, {"exp": float('inf')}, {"exp": None}])
def test_malformed_signed_cookie_does_not_raise(body):
    settings = Settings(_env_file=None, app_secret_key="test-private-key")
    payload = json.dumps(body).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    assert not session_is_valid(f"{encoded}.{_sign(payload, settings.session_signing_key)}", settings)


def test_non_ascii_signature_and_password_are_safe():
    settings = Settings(_env_file=None, admin_password="private-test-password")
    token = issue_session(settings)
    assert not session_is_valid(token.partition(".")[0] + ".\u2603", settings)
    assert not password_matches("\u2603", settings)


@pytest.mark.asyncio
async def test_production_internal_api_fails_closed_without_key():
    with pytest.raises(HTTPException) as error:
        await require_internal_api_key(None, Settings(_env_file=None, app_env="production", internal_api_key=""))
    assert error.value.status_code == 503


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_runtime_audio_settings_reject_nonfinite(value):
    with pytest.raises(ConfigError):
        coerce("ai_tts_speed", value)


def test_pdf_normalization_removes_nul_preserves_hindi():
    assert normalize_text("लम्पी\x00 रोग\n\nलक्षण") == "लम्पी रोग\n\nलक्षण"


def test_third_turn_keeps_explicit_topic():
    query = build_retrieval_query("aur bachav?", ["lumpy skin disease", "iska ilaj kya hai?", "haanji"])
    assert query == "lumpy skin disease aur bachav?"


def test_urls_are_not_read_to_the_caller():
    from app.services.spoken_text import spoken_text
    assert spoken_text("जानकारी https://example.invalid/page देखें।") == "जानकारी देखें।"


@pytest.mark.asyncio
async def test_playback_finished_before_http_response_is_not_lost(tmp_path):
    controller, ari = _make_controller(tmp_path)
    state = CallState("race", uuid.uuid4(), uuid.uuid4())

    async def immediate_finish(channel_id, *, media, playback_id=None):
        controller._on_playback_finished({"playback": {"id": playback_id}})
        return {"id": playback_id}

    ari.play = immediate_finish
    await asyncio.wait_for(controller._play_and_wait(state, media="sound:test"), timeout=0.25)
    assert not controller._playback_finished
    assert state.active_playback_id is None


@pytest.mark.asyncio
async def test_cancel_during_playback_request_stops_and_cleans_waiter(tmp_path):
    controller, ari = _make_controller(tmp_path)
    state = CallState("cancel-request")
    entered = asyncio.Event()

    async def pending_play(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    ari.play = pending_play
    task = asyncio.create_task(controller._play_and_wait(state, media="sound:test"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    playback_id = state.active_playback_id
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ari.stopped_playbacks == [playback_id]
    assert not controller._playback_finished
    assert state.active_playback_id is None


@pytest.mark.asyncio
async def test_playback_request_failure_cleans_waiter(tmp_path):
    from app.services.ari_client import AriError
    controller, ari = _make_controller(tmp_path)
    state = CallState("failed-request")

    async def failed_play(*args, **kwargs):
        raise AriError("connection failed")

    ari.play = failed_play
    with pytest.raises(AriError):
        await controller._play_and_wait(state, media="sound:test")
    assert not controller._playback_finished
    assert state.active_playback_id is None


@pytest.mark.asyncio
async def test_cancelled_call_wait_drains_provider_cleanup(tmp_path):
    controller, _ = _make_controller(tmp_path)
    entered, cleaned = asyncio.Event(), asyncio.Event()

    async def work():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = asyncio.create_task(controller._unless_call_ends(CallState("cancel-turn"), work()))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()


@pytest.mark.asyncio
async def test_duplicate_recording_finished_is_claimed_once(tmp_path):
    controller, ari = _make_controller(tmp_path)
    state = CallState("dedup", uuid.uuid4(), uuid.uuid4())
    controller._calls[state.channel_id] = state
    state.recording_seq = 1
    name = f"ai-agent__{state.session_id}__1"
    (tmp_path / f"{name}.wav").write_bytes(b"caller question")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def turn(*args, **kwargs):
        calls.append(1)
        entered.set()
        await release.wait()

    controller._run_turn = turn
    first = asyncio.create_task(controller._on_recording_finished(recording_finished_event(name)))
    await entered.wait()
    await controller._on_recording_finished(recording_finished_event(name))
    release.set()
    await first
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_call_audio_settings_are_snapshotted(tmp_path):
    controller, ari = _make_controller(tmp_path)
    state = CallState("snapshot")
    state.settings = controller._settings.model_copy(update={"ai_max_turn_seconds": 11})
    controller._settings = controller._settings.model_copy(update={"ai_max_turn_seconds": 19})
    await controller._start_next_recording(state)
    assert ari.record_params[-1]["max_duration_seconds"] == 11


@pytest.mark.asyncio
async def test_generated_speech_files_are_removed_after_playback(tmp_path):
    from app.services.audio import make_short_beep_wav
    controller, ari = _make_controller(tmp_path)
    state = CallState("cleanup", uuid.uuid4(), uuid.uuid4())
    await controller._write_and_play_wav(state, make_short_beep_wav(), audio_format="wav")
    assert not list((tmp_path.parent / "sounds" / "ai-agent").glob(f"{state.session_id}-*.wav"))
