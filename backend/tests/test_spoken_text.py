"""What reaches the TTS engine must be speech, not markup.

The prompt asks the model for plain spoken Hindi, but a prompt is not a
guarantee — these cover what happens when the model formats an answer anyway.
"""

import pytest

from app.services.call_controller import speech_chunks
from app.services.spoken_text import spoken_text


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("**टीका** लगवाएं", "टीका लगवाएं"),
        ("*टीका* लगवाएं", "टीका लगवाएं"),
        ("`टीका` लगवाएं", "टीका लगवाएं"),
        ("__टीका__ लगवाएं", "टीका लगवाएं"),
        ("### बचाव के उपाय", "बचाव के उपाय"),
        ("> पशु को अलग रखें", "पशु को अलग रखें"),
        ("[यहाँ देखें](http://example.com)", "यहाँ देखें"),
    ],
)
def test_markup_is_removed_but_the_words_are_kept(raw, expected) -> None:
    assert spoken_text(raw) == expected


def test_an_unbalanced_marker_does_not_survive() -> None:
    assert spoken_text("**टीका लगवाएं") == "टीका लगवाएं"


def test_list_items_become_spoken_sentences() -> None:
    reply = "- पशु को अलग रखें\n- मक्खियों को नियंत्रित करें"
    assert spoken_text(reply) == "पशु को अलग रखें। मक्खियों को नियंत्रित करें।"


def test_numbered_items_lose_the_number_but_keep_the_sentence() -> None:
    assert spoken_text("1. टीका लगवाएं\n2. डॉक्टर को दिखाएं") == "टीका लगवाएं। डॉक्टर को दिखाएं।"


def test_a_decimal_number_mid_sentence_is_not_mistaken_for_a_list() -> None:
    assert spoken_text("रोज़ 2.5 लीटर पानी दें।") == "रोज़ 2.5 लीटर पानी दें।"


def test_an_item_that_already_ends_in_punctuation_is_not_given_another() -> None:
    assert spoken_text("- पशु को अलग रखें।\n- मक्खी हटाएं?") == "पशु को अलग रखें। मक्खी हटाएं?"


def test_emoji_and_pictographs_never_reach_the_engine() -> None:
    assert spoken_text("ठीक है 😊👍 धन्यवाद।") == "ठीक है धन्यवाद।"


def test_newlines_become_spaces_so_nothing_runs_together() -> None:
    assert spoken_text("नमस्ते।\n\nबताइए क्या जानना है?") == "नमस्ते। बताइए क्या जानना है?"


def test_a_markdown_table_does_not_get_read_out() -> None:
    reply = "जानकारी:\n| ज़िला | केंद्र |\n| --- | --- |\n| हिसार | लुवास |"
    assert spoken_text(reply) == "जानकारी:"


def test_repeated_punctuation_is_collapsed() -> None:
    assert spoken_text("ठीक है।। बताइए!!") == "ठीक है। बताइए!"


def test_plain_hindi_is_left_exactly_as_it_is() -> None:
    reply = "जी हाँ, लम्पी रोग मक्खियों और मच्छरों से फैलता है। क्या आपके पशु में गांठें हैं?"
    assert spoken_text(reply) == reply


def test_empty_and_whitespace_input_are_safe() -> None:
    assert spoken_text("") == ""
    assert spoken_text("   \n  ") == ""


def test_the_speech_chunker_never_emits_markup(monkeypatch) -> None:
    """The chunker is the only path into TTS, so cleaning happens there."""
    reply = "### उपाय\n\n1. **टीका** लगवाएं\n2. मक्खियों को नियंत्रित करें\n\nडॉक्टर से मिलें।"
    chunks = speech_chunks(reply)

    assert chunks
    joined = " ".join(chunks)
    for marker in ("*", "#", "`", "1.", "2."):
        assert marker not in joined
    assert "टीका लगवाएं" in joined
    assert "डॉक्टर से मिलें।" in joined
