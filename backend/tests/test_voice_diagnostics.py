import pytest

from app.services.audio import AudioFormatError, read_wav_info
from app.services.voice_diagnostics import build_voice_ab_variants, describe_wav, g711_roundtrip
from tests.audio_fixtures import make_tone_wav


def test_ab_variants_preserve_original_and_isolate_tempo() -> None:
    source = make_tone_wav(duration_seconds=3.0, sample_rate=48000, amplitude=8000)
    variants = build_voice_ab_variants(source, current_speed=1.15)

    assert variants["A_original_bhashini.wav"] == source
    for name, wav in variants.items():
        info = read_wav_info(wav)
        if name.startswith("A_"):
            assert info.sample_rate == 48000
        else:
            assert (info.sample_rate, info.channels, info.sample_width) == (8000, 1, 2)

    current = read_wav_info(variants["B1_current_8k.wav"])
    candidate = read_wav_info(variants["B2_candidate_native_cadence_8k.wav"])
    assert current.duration_seconds == pytest.approx(candidate.duration_seconds / 1.15, rel=0.04)


@pytest.mark.parametrize("codec", ["ulaw", "alaw"])
def test_g711_preview_is_telephone_pcm_without_clipping(codec: str) -> None:
    source = make_tone_wav(duration_seconds=1.0, sample_rate=8000, amplitude=10000)
    preview = g711_roundtrip(source, codec=codec)
    metrics = describe_wav(preview, word_count=3)

    assert metrics["sample_rate_hz"] == 8000
    assert metrics["channels"] == 1
    assert metrics["sample_width_bits"] == 16
    assert metrics["duration_seconds"] == pytest.approx(1.0, abs=0.01)
    assert metrics["clipped_ratio"] == 0.0
    assert metrics["words_per_minute"] == pytest.approx(180.0)


def test_g711_preview_rejects_non_telephone_input() -> None:
    with pytest.raises(AudioFormatError, match="8kHz mono 16-bit"):
        g711_roundtrip(make_tone_wav(sample_rate=48000), codec="ulaw")


def test_g711_preview_rejects_unknown_codec() -> None:
    with pytest.raises(ValueError, match="codec"):
        g711_roundtrip(make_tone_wav(), codec="gsm")
