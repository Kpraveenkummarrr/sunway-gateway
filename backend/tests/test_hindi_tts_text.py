"""What a Hindi voice is given to say.

The approved welcome message is pure Devanagari and sounds clear. The AI's
replies, written by an LLM, carry digits, units, percentages, brackets and
Latin acronyms that an Indic voice skips or mangles - a plausible contributor
to "the welcome is clear but the answers are not". Everything is rewritten as
spoken Devanagari before synthesis; text that needs nothing is returned as is.
"""

import pytest

from app.services.hindi_tts_text import hindi_tts_text, number_to_hindi, unspoken_latin


@pytest.mark.parametrize(
    "number, spoken",
    [(0, "शून्य"), (1, "एक"), (19, "उन्नीस"), (21, "इक्कीस"), (99, "निन्यानवे"), (100, "एक सौ"),
     (250, "दो सौ पचास"), (1000, "एक हज़ार"), (1200, "एक हज़ार दो सौ"), (100000, "एक लाख")],
)
def test_numbers_are_read_as_hindi_words(number, spoken) -> None:
    assert number_to_hindi(number) == spoken


@pytest.mark.parametrize(
    "written, spoken",
    [
        ("50 मिलीलीटर दवा दिन में 3 बार दें", "पचास मिलीलीटर दवा दिन में तीन बार दें"),
        ("2-3 दिन में टीका लगवाएं", "दो से तीन दिन में टीका लगवाएं"),
        ("लगभग 25% पशु प्रभावित होते हैं", "लगभग पच्चीस प्रतिशत पशु प्रभावित होते हैं"),
        ("₹500 खर्च होते हैं", "पाँच सौ रुपये खर्च होते हैं"),
        ("तापमान 39.5 °C है", "तापमान उनतालीस दशमलव पाँच डिग्री सेल्सियस है"),
        ("बुखार 104°F तक हो सकता है", "बुखार एक सौ चार डिग्री फ़ारेनहाइट तक हो सकता है"),
        ("1,200 पशु", "एक हज़ार दो सौ पशु"),
        ("१०० पशु और ५ बार", "एक सौ पशु और पाँच बार"),
        ("0 से 21 दिन", "शून्य से इक्कीस दिन"),
    ],
)
def test_digits_units_and_symbols_become_spoken_words(written, spoken) -> None:
    assert hindi_tts_text(written) == spoken


def test_acronyms_are_spoken_the_way_a_farmer_says_them() -> None:
    assert hindi_tts_text("NDDB और LUVAS से संपर्क करें") == "एन डी डी बी और लुवास से संपर्क करें"
    assert "एल एस डी" in hindi_tts_text("लम्पी स्किन डिजीज (LSD) रोग है")
    assert hindi_tts_text("ICAR के अनुसार") == "आई सी ए आर के अनुसार"


def test_an_unknown_acronym_is_spelled_letter_by_letter() -> None:
    assert hindi_tts_text("XYZ जांच") == "एक्स वाई ज़ेड जांच"


def test_a_bracketed_aside_becomes_a_pause_not_a_bracket() -> None:
    assert hindi_tts_text("इलाज (पशु चिकित्सक की सलाह से) करें") == "इलाज, पशु चिकित्सक की सलाह से, करें"


def test_pure_devanagari_is_returned_unchanged() -> None:
    """The approved welcome must not be altered by a single character."""
    welcome = "आपसे बात करके अच्छा लगा। और जानकारी के लिए आप कभी भी दोबारा कॉल कर सकते हैं। धन्यवाद।"
    assert hindi_tts_text(welcome) is welcome


def test_empty_text_is_returned_as_is() -> None:
    assert hindi_tts_text("") == ""


def test_no_latin_word_is_left_for_the_voice_to_mangle_in_a_typical_reply() -> None:
    reply = "लम्पी स्किन डिजीज (LSD) में 104°F बुखार होता है। NDDB की सलाह से 3 दिन में टीका लगवाएं।"
    assert unspoken_latin(reply) == []
    assert not any(ch.isascii() and ch.isalnum() for ch in hindi_tts_text(reply))


def test_an_unglossed_latin_word_is_reported_so_it_can_be_added() -> None:
    assert unspoken_latin("यह Ivermectin दवा है") == ["Ivermectin"]
