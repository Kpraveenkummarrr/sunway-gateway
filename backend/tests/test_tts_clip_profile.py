"""The clean audio profile: what the caller hears from a synthesized reply.

The complaint was noisy / robotic replies while the welcome message was clear.
Both go through the same processing, so it was made minimal by default: one
band-limited resample, NO tempo processing at speed 1.0, loudness matched by the
speech itself with a hard peak ceiling. The legacy chain is kept byte-identical
so the two can be compared on the same text with one setting.
"""

import numpy as np
import pytest

from app.services import audio
from app.services.audio import (
    AUDIO_PROFILES,
    AudioFormatError,
    measure_levels,
    prepare_tts_clip,
    prepare_tts_for_playback,
    read_wav_info,
)
from tests.audio_fixtures import make_silence_wav, make_tone_wav


def _out_info(clip):
    return read_wav_info(clip.audio)


def test_the_output_is_always_8khz_mono_16_bit_for_asterisk() -> None:
    clip = prepare_tts_clip(make_tone_wav(duration_seconds=1.0, sample_rate=48000))
    info = _out_info(clip)
    assert (info.sample_rate, info.channels, info.sample_width) == (8000, 1, 2)
    assert clip.diagnostics.resampled is True
    assert clip.diagnostics.final_rate == 8000


def test_a_source_that_is_already_8khz_is_not_resampled() -> None:
    clip = prepare_tts_clip(make_tone_wav(duration_seconds=1.0, sample_rate=8000))
    assert clip.diagnostics.resampled is False


def test_speed_one_runs_no_tempo_processing_at_all(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("time_stretch must not run at speed 1.0")

    monkeypatch.setattr(audio, "time_stretch", forbidden)
    clip = prepare_tts_clip(make_tone_wav(duration_seconds=1.0, sample_rate=48000), speed=1.0)
    assert clip.diagnostics.tempo_applied is False
    assert clip.diagnostics.tempo == 1.0


def test_a_speed_within_one_percent_of_one_is_also_left_alone(monkeypatch) -> None:
    monkeypatch.setattr(audio, "time_stretch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("stretched")))
    assert prepare_tts_clip(make_tone_wav(sample_rate=48000), speed=1.005).diagnostics.tempo_applied is False


def test_an_explicit_speed_still_changes_the_pace() -> None:
    source = make_tone_wav(duration_seconds=2.0, sample_rate=48000)
    normal = prepare_tts_clip(source, speed=1.0)
    faster = prepare_tts_clip(source, speed=1.15)
    assert faster.diagnostics.tempo_applied is True
    ratio = faster.diagnostics.final_duration_s / normal.diagnostics.final_duration_s
    assert ratio == pytest.approx(1 / 1.15, abs=0.04)


def test_nothing_can_clip_however_hot_the_source_is() -> None:
    clip = prepare_tts_clip(make_tone_wav(duration_seconds=1.0, sample_rate=48000, amplitude=32700))
    levels = measure_levels(clip.audio)
    assert levels.clipped_ratio == 0.0
    assert levels.peak_dbfs <= -2.5  # the -3 dBFS ceiling, plus rounding


def test_replies_of_different_source_loudness_come_out_equally_loud() -> None:
    """Consecutive chunks of one reply must not jump in volume."""
    quiet = prepare_tts_clip(make_tone_wav(duration_seconds=1.0, sample_rate=48000, amplitude=3000))
    loud = prepare_tts_clip(make_tone_wav(duration_seconds=1.0, sample_rate=48000, amplitude=14000))
    assert abs(quiet.diagnostics.rms_dbfs - loud.diagnostics.rms_dbfs) < 3.0


def test_the_clip_starts_and_ends_at_zero_so_it_cannot_click() -> None:
    samples = np.frombuffer(prepare_tts_clip(make_tone_wav(sample_rate=48000)).audio[44:], dtype="<i2")
    assert abs(int(samples[0])) < 200 and abs(int(samples[-1])) < 200


def test_silent_synthesis_is_rejected_not_played() -> None:
    with pytest.raises(AudioFormatError):
        prepare_tts_clip(make_silence_wav(duration_seconds=1.0, sample_rate=48000))


def test_an_unknown_profile_is_refused() -> None:
    with pytest.raises(ValueError):
        prepare_tts_clip(make_tone_wav(), profile="turbo")


def test_the_two_profiles_are_the_documented_ones() -> None:
    assert set(AUDIO_PROFILES) == {"clean", "legacy"}


@pytest.mark.parametrize("speed", [1.0, 1.15])
def test_the_legacy_profile_reproduces_the_previous_chain_byte_for_byte(speed) -> None:
    source = make_tone_wav(duration_seconds=1.0, sample_rate=48000)
    assert prepare_tts_clip(source, speed=speed, profile="legacy").audio == prepare_tts_for_playback(source, speed=speed)


def test_every_clip_carries_the_diagnostics_that_locate_a_bad_sound() -> None:
    clip = prepare_tts_clip(make_tone_wav(duration_seconds=1.0, sample_rate=48000))
    summary = clip.diagnostics.summary()
    for field in ("profile=clean", "src=48000Hz", "out=8000Hz", "tempo=1.00x", "tempo_applied=False",
                  "gain=", "peak=", "rms=", "clipped=", "noise_floor="):
        assert field in summary, (field, summary)
