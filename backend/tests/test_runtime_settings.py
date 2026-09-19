"""Defaults and operator-editable settings for the voice path.

Two things went wrong on the client's machine that these guard against:
  * the example .env pinned AI_CALL_TIMEOUT_SECONDS=120 and the client copied it,
    so a wrong default in the example silently overrode the code's default;
  * a value nobody could change without editing code (barge-in threshold, the
    call limit) had to be guessed.
So every setting this work added must be in .env.example with the same value as
the code default, and the panel must only ever offer non-secret, bounded ones.
"""

import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.system_config import EDITABLE_KEYS, ConfigError, EditableSetting, coerce

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

# The settings this work added or re-defaulted, and what production should run with.
EXPECTED_DEFAULTS = {
    "ai_call_timeout_seconds": 900,
    "ai_end_of_speech_silence_seconds": 2,
    "ai_max_turn_seconds": 20,
    "ai_endpoint_monitor": True,
    "ai_endpoint_silence_ms": 1200,
    "ai_endpoint_min_speech_ms": 250,
    "ai_endpoint_use_rtp_statistics": True,
    "ai_talk_detect_override": True,
    "ai_talk_detect_threshold": 350,
    "ai_talk_detect_silence_ms": 200,
    "ai_tts_speed": 1.0,
    "ai_audio_profile": "clean",
    "ai_tts_target_rms_dbfs": -18.0,
    "ai_tts_peak_ceiling_dbfs": -3.0,
    "ai_max_context_chars": 3600,
}


def _env_example() -> dict[str, str]:
    values = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


@pytest.mark.parametrize("key, expected", EXPECTED_DEFAULTS.items())
def test_the_code_defaults_are_the_ones_production_needs(key, expected) -> None:
    assert getattr(Settings(_env_file=None), key) == expected


@pytest.mark.parametrize("key, expected", EXPECTED_DEFAULTS.items())
def test_the_example_env_agrees_with_the_code_defaults(key, expected) -> None:
    """A stale example is dangerous: it is copied to .env and then overrides the code."""
    written = _env_example().get(key.upper())
    assert written is not None, f"{key.upper()} is missing from .env.example"
    if isinstance(expected, bool):
        assert written.lower() == str(expected).lower()
    elif isinstance(expected, str):
        assert written == expected
    else:
        assert float(written) == float(expected)


def test_the_example_env_holds_no_real_credentials() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert not re.search(r"\b(sk-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_\-]{20,}|AKIA[0-9A-Z]{12,})", text)
    values = _env_example()
    for key in ("GEMINI_API_KEY", "LLM_API_KEY", "STT_API_KEY", "TTS_API_KEY", "BHASHINI_INFERENCE_API_KEY",
                "ASTERISK_ARI_PASSWORD", "INTERNAL_API_KEY", "ADMIN_PASSWORD", "APP_SECRET_KEY"):
        assert values.get(key, "") == "", f"{key} must be empty in the example"


# ---- what the admin panel may change ----


def test_the_panel_offers_the_new_operational_settings_with_sane_bounds() -> None:
    assert coerce("ai_audio_profile", "legacy") == "legacy"
    assert coerce("ai_call_timeout_seconds", "900") == 900
    assert coerce("ai_call_timeout_seconds", "0") == 0  # no limit
    assert coerce("ai_endpoint_silence_ms", "1200") == 1200
    assert coerce("ai_talk_detect_threshold", "350") == 350
    assert coerce("ai_tts_speed", "1.0") == 1.0
    for key, bad in (("ai_audio_profile", "turbo"), ("ai_call_timeout_seconds", "-1"), ("ai_call_timeout_seconds", "99999"),
                     ("ai_endpoint_silence_ms", "399"), ("ai_endpoint_silence_ms", "4001"),
                     ("ai_talk_detect_threshold", "50"), ("ai_tts_peak_ceiling_dbfs", "0"), ("ai_tts_speed", "nan")):
        with pytest.raises(ConfigError):
            coerce(key, bad)


def test_the_master_switches_and_provider_choices_are_not_editable_from_the_panel() -> None:
    for key in ("ai_endpoint_monitor", "ai_endpoint_use_rtp_statistics", "ai_talk_detect_override",
                "llm_provider", "stt_provider", "tts_provider", "rag_embedding_provider"):
        assert key not in EDITABLE_KEYS, f"{key} must stay a deployment decision"


def test_nothing_secret_can_ever_be_offered_for_editing() -> None:
    # "max_tokens" is an answer-length limit, not an authentication token.
    forbidden = re.compile(
        r"(api_?key|password|secret|access_token|refresh_token|credential|url|dsn|database)",
        re.IGNORECASE,
    )
    assert not [k for k in EDITABLE_KEYS if forbidden.search(k)], "a secret-like setting became editable"


def test_boolean_settings_parse_the_words_a_form_sends(monkeypatch) -> None:
    monkeypatch.setitem(EDITABLE_KEYS, "flag", EditableSetting("flag", bool, "test flag"))
    for word in ("true", "True", "1", "yes", "on"):
        assert coerce("flag", word) is True
    for word in ("false", "False", "0", "no", "off", ""):
        assert coerce("flag", word) is False  # bool("false") would be True
    with pytest.raises(ConfigError):
        coerce("flag", "maybe")
