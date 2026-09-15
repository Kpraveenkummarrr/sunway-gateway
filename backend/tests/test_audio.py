import pytest

from app.services.audio import (
    AudioFormatError,
    is_asterisk_playable,
    is_effectively_silent,
    is_too_short,
    normalize_for_asterisk_playback,
    read_wav_info,
)
from tests.audio_fixtures import make_empty_wav, make_noise_wav, make_silence_wav, make_tone_wav


def test_read_wav_info_reports_correct_metadata() -> None:
    wav = make_tone_wav(duration_seconds=1.0, sample_rate=8000, channels=1)
    info = read_wav_info(wav)
    assert info.sample_rate == 8000
    assert info.channels == 1
    assert info.sample_width == 2
    assert info.duration_seconds == pytest.approx(1.0, abs=0.01)


def test_read_wav_info_rejects_non_wav_bytes() -> None:
    with pytest.raises(AudioFormatError):
        read_wav_info(b"not a wav file at all")


def test_8khz_mono_16bit_is_playable() -> None:
    wav = make_tone_wav(sample_rate=8000, channels=1)
    assert is_asterisk_playable(read_wav_info(wav)) is True


def test_16khz_mono_16bit_is_playable() -> None:
    wav = make_tone_wav(sample_rate=16000, channels=1)
    assert is_asterisk_playable(read_wav_info(wav)) is True


def test_24khz_is_not_playable() -> None:
    """Matches OpenAI TTS's native wav output rate — confirmed via
    `asterisk -rx "module show like format"` that format_wav.so only
    supports 8kHz/16kHz."""
    wav = make_tone_wav(sample_rate=24000, channels=1)
    assert is_asterisk_playable(read_wav_info(wav)) is False


def test_stereo_is_not_playable() -> None:
    wav = make_tone_wav(sample_rate=8000, channels=2)
    assert is_asterisk_playable(read_wav_info(wav)) is False


def test_normalize_is_noop_for_already_playable_audio() -> None:
    wav = make_tone_wav(sample_rate=8000, channels=1)
    assert normalize_for_asterisk_playback(wav) == wav


def test_normalize_downsamples_24khz_to_8khz() -> None:
    wav_24k = make_tone_wav(duration_seconds=1.0, sample_rate=24000, channels=1)
    normalized = normalize_for_asterisk_playback(wav_24k)
    info = read_wav_info(normalized)
    assert info.sample_rate == 8000
    assert info.channels == 1
    assert info.sample_width == 2
    assert is_asterisk_playable(info) is True
    # Duration should be preserved across the resample.
    assert info.duration_seconds == pytest.approx(1.0, abs=0.05)


def test_normalize_downmixes_stereo_to_mono() -> None:
    wav_stereo = make_tone_wav(duration_seconds=0.5, sample_rate=8000, channels=2)
    normalized = normalize_for_asterisk_playback(wav_stereo)
    info = read_wav_info(normalized)
    assert info.channels == 1
    assert is_asterisk_playable(info) is True


def test_silence_is_detected_as_silent() -> None:
    wav = make_silence_wav(duration_seconds=1.0)
    assert is_effectively_silent(wav) is True


def test_tone_is_not_detected_as_silent() -> None:
    wav = make_tone_wav(duration_seconds=1.0, amplitude=12000)
    assert is_effectively_silent(wav) is False


def test_noise_is_not_detected_as_silent() -> None:
    wav = make_noise_wav(duration_seconds=1.0)
    assert is_effectively_silent(wav) is False


def test_empty_wav_is_silent() -> None:
    wav = make_empty_wav()
    assert is_effectively_silent(wav) is True


def test_short_audio_is_flagged_too_short() -> None:
    wav = make_tone_wav(duration_seconds=0.1)
    assert is_too_short(read_wav_info(wav)) is True


def test_normal_audio_is_not_too_short() -> None:
    wav = make_tone_wav(duration_seconds=2.0)
    assert is_too_short(read_wav_info(wav)) is False


# ---- telephone audio quality and latency processing ----

import io  # noqa: E402
import wave  # noqa: E402

import numpy as np  # noqa: E402

from app.services.audio import (  # noqa: E402
    measure_levels,
    prepare_for_asr,
    prepare_tts_for_playback,
    time_stretch,
)


def _wav_from(samples: np.ndarray, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.clip(np.round(samples * 32767), -32768, 32767).astype("<i2").tobytes())
    return buf.getvalue()


def _samples(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        return np.frombuffer(wf.readframes(wf.getnframes()), "<i2").astype(float) / 32768, wf.getframerate()


def _magnitude_at(samples: np.ndarray, rate: int, hz: float) -> float:
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return float(spectrum[np.argmin(np.abs(np.fft.rfftfreq(len(samples), 1 / rate) - hz))])


def _tone(hz: float, seconds: float, rate: int, amplitude: float = 0.3) -> np.ndarray:
    return amplitude * np.sin(2 * np.pi * hz * np.arange(int(seconds * rate)) / rate)


def test_downsampling_does_not_alias_high_frequencies_into_the_phone_band() -> None:
    """48kHz TTS -> 8kHz: a 6kHz component (sibilant energy) must not fold
    down to 2kHz. The old audioop.ratecv path passed it at 0 dB."""
    source = _wav_from(_tone(1000, 2.0, 48000) + _tone(6000, 2.0, 48000), 48000)
    out, rate = _samples(normalize_for_asterisk_playback(source))
    assert rate == 8000
    alias_db = 20 * np.log10(_magnitude_at(out, rate, 2000) / _magnitude_at(out, rate, 1000))
    assert alias_db < -60


def test_telephone_band_is_preserved_by_resampling() -> None:
    out, _ = _samples(normalize_for_asterisk_playback(_wav_from(_tone(3000, 1.0, 48000, amplitude=0.5), 48000)))
    assert np.max(np.abs(out[800:-800])) == pytest.approx(0.5, abs=0.02)


def test_prepare_tts_trims_silence_and_outputs_8khz_mono_pcm() -> None:
    rate = 48000
    speech = _tone(300, 2.0, rate) * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * np.arange(2 * rate) / rate))
    source = _wav_from(np.concatenate([np.zeros(rate), speech, np.zeros(rate)]), rate)

    prepared = prepare_tts_for_playback(source, speed=1.0)

    info = read_wav_info(prepared)
    assert (info.sample_rate, info.channels, info.sample_width) == (8000, 1, 2)
    assert info.duration_seconds == pytest.approx(2.1, abs=0.1)  # 1 s silence each side -> 60 ms pads
    levels = measure_levels(prepared)
    assert levels.leading_silence_seconds < 0.1 and levels.trailing_silence_seconds < 0.1
    assert levels.peak_dbfs == pytest.approx(-3.0, abs=0.2)
    assert levels.clipped_ratio == 0.0


def test_prepare_tts_speeds_up_speech_without_changing_pitch() -> None:
    source = _wav_from(_tone(440, 3.0, 48000), 48000)
    normal = prepare_tts_for_playback(source, speed=1.0)
    faster = prepare_tts_for_playback(source, speed=1.2)

    assert read_wav_info(faster).duration_seconds == pytest.approx(read_wav_info(normal).duration_seconds / 1.2, rel=0.03)
    samples, rate = _samples(faster)
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    assert np.fft.rfftfreq(len(samples), 1 / rate)[np.argmax(spectrum)] == pytest.approx(440, abs=5)


def test_prepare_tts_never_clips_hot_audio() -> None:
    hot = _wav_from(np.clip(_tone(500, 1.0, 48000, amplitude=1.4), -1, 1), 48000)
    levels = measure_levels(prepare_tts_for_playback(hot))
    assert levels.clipped_ratio == 0.0
    assert levels.peak_dbfs <= -2.8


def test_prepare_tts_rejects_silent_audio() -> None:
    with pytest.raises(AudioFormatError, match="silent"):
        prepare_tts_for_playback(_wav_from(np.zeros(48000), 48000))


def test_prepare_for_asr_trims_pause_and_end_of_speech_silence() -> None:
    rate = 8000
    recording = np.concatenate([np.zeros(rate), _tone(400, 1.0, rate), np.zeros(2 * rate)])  # pause, speech, 2 s silence
    prepared = prepare_for_asr(_wav_from(recording, rate))

    info = read_wav_info(prepared)
    assert (info.sample_rate, info.channels, info.sample_width) == (16000, 1, 2)
    assert info.duration_seconds == pytest.approx(1.5, abs=0.1)  # 1 s speech + 250 ms context each side


def test_prepare_for_asr_keeps_short_audio_untrimmed() -> None:
    prepared = prepare_for_asr(make_tone_wav(duration_seconds=0.2, sample_rate=8000))
    assert read_wav_info(prepared).duration_seconds == pytest.approx(0.2, abs=0.01)


def test_time_stretch_is_identity_at_normal_speed() -> None:
    samples = _tone(440, 1.0, 8000)
    assert time_stretch(samples, 8000, 1.0) is samples
