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
