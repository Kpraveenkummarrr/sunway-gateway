"""Text a Hindi TTS voice can read: Devanagari words, no digits, no Latin acronyms.

Why this exists
---------------
The welcome message is hand-written Devanagari prose, and it sounds clear. The
model's replies are not: they carry digits ("2-3 दिन"), units ("5 ml"),
percentages, brackets, and Latin acronyms ("LSD", "NDDB"). An Indic TTS voice
has no reliable way to read those - depending on the engine it skips them,
reads them in English, or produces a burst of noise where the input falls
outside what it was trained on. That is a difference in *what is fed to the
voice*, not in how the audio is processed afterwards (welcome and replies go
through exactly the same audio path).

This turns the risky parts into Devanagari words. Pure Devanagari text - the
welcome message, most of a good reply - passes through unchanged, byte for byte.
Latin words that are not in the glossary are left as they are (guessing a
transliteration would be worse than leaving them) but are reported by
`unspoken_latin()` so they can be logged and added.
"""

from __future__ import annotations

import re

_WORDS_0_99 = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस "
    "ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस बीस "
    "इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस "
    "इकतीस बत्तीस तैंतीस चौंतीस पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस "
    "इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस सैंतालीस अड़तालीस उनचास पचास "
    "इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ "
    "इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर "
    "इकहत्तर बहत्तर तिहत्तर चौहत्तर पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी "
    "इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी अट्ठासी नवासी नब्बे "
    "इक्यानवे बानवे तिरानवे चौरानवे पंचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()
assert len(_WORDS_0_99) == 100

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Units that follow a number. Longest first so "mg" is not read as "m" + "g".
_UNITS = {
    "mg": "मिलीग्राम",
    "ml": "मिलीलीटर",
    "kg": "किलो",
    "gm": "ग्राम",
    "g": "ग्राम",
    "cm": "सेंटीमीटर",
    "mm": "मिलीमीटर",
    "l": "लीटर",
    "°c": "डिग्री सेल्सियस",
    "°f": "डिग्री फ़ारेनहाइट",
    "°": "डिग्री",
}

# Acronyms and terms this helpline actually uses, spoken the way a farmer says
# them. Anything else in capitals is spelled out letter by letter.
_GLOSSARY = {
    "LSD": "एल एस डी",
    "NDDB": "एन डी डी बी",
    "LUVAS": "लुवास",
    "ICAR": "आई सी ए आर",
    "DDL": "डी डी एल",
    "AI": "ए आई",
    "PDF": "पी डी एफ",
    "SMS": "एस एम एस",
    "OK": "ठीक है",
    "virus": "वायरस",
    "vaccine": "वैक्सीन",
    "vaccination": "टीकाकरण",
    "ok": "ठीक है",
}

_LETTERS = {
    "A": "ए", "B": "बी", "C": "सी", "D": "डी", "E": "ई", "F": "एफ", "G": "जी", "H": "एच", "I": "आई",
    "J": "जे", "K": "के", "L": "एल", "M": "एम", "N": "एन", "O": "ओ", "P": "पी", "Q": "क्यू", "R": "आर",
    "S": "एस", "T": "टी", "U": "यू", "V": "वी", "W": "डब्ल्यू", "X": "एक्स", "Y": "वाई", "Z": "ज़ेड",
}

_UNIT_PATTERN = "|".join(sorted((re.escape(u) for u in _UNITS), key=len, reverse=True))
_NUMBER = r"\d+(?:,\d{2,3})*(?:\.\d+)?"
_RANGE = re.compile(rf"(?<![\w.])(\d+)\s*[-–—]\s*(\d+)(?![\w.])")
_PERCENT = re.compile(rf"(?<![\w.])({_NUMBER})\s*%")
_CURRENCY = re.compile(rf"(?:₹|Rs\.?\s*)\s*({_NUMBER})")
_WITH_UNIT = re.compile(rf"(?<![\w.])({_NUMBER})\s*({_UNIT_PATTERN})(?![A-Za-z])", re.IGNORECASE)
_PLAIN_NUMBER = re.compile(rf"(?<![\w.])({_NUMBER})(?![\w])")
_ACRONYM = re.compile(r"(?<![A-Za-z])[A-Z]{2,6}(?![A-Za-z])")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z']*")
_BRACKETS = re.compile(r"\s*[()\[\]{}]\s*")
_SLASH = re.compile(r"\s*/\s*")


def number_to_hindi(n: int) -> str:
    """0 -> शून्य, 21 -> इक्कीस, 120 -> एक सौ बीस, 2,500 -> दो हज़ार पाँच सौ."""
    if n < 0:
        return "ऋण " + number_to_hindi(-n)
    if n < 100:
        return _WORDS_0_99[n]
    parts: list[str] = []
    crore, rest = divmod(n, 10_000_000)
    lakh, rest = divmod(rest, 100_000)
    thousand, rest = divmod(rest, 1000)
    hundred, rest = divmod(rest, 100)
    if crore:
        parts.append(f"{number_to_hindi(crore)} करोड़")
    if lakh:
        parts.append(f"{number_to_hindi(lakh)} लाख")
    if thousand:
        parts.append(f"{number_to_hindi(thousand)} हज़ार")
    if hundred:
        parts.append(f"{number_to_hindi(hundred)} सौ")
    if rest:
        parts.append(_WORDS_0_99[rest])
    return " ".join(parts)


def _spoken_number(text: str) -> str:
    """'2.5' -> 'दो दशमलव पाँच', '1,200' -> 'एक हज़ार दो सौ'."""
    whole, _, fraction = text.replace(",", "").partition(".")
    words = number_to_hindi(int(whole))
    if fraction:
        words += " दशमलव " + " ".join(_WORDS_0_99[int(d)] for d in fraction)
    return words


def _spell(match: re.Match) -> str:
    word = match.group(0)
    if word in _GLOSSARY:
        return _GLOSSARY[word]
    return " ".join(_LETTERS[c] for c in word)


def hindi_tts_text(text: str) -> str:
    """`text` with digits, units, percentages, currency, brackets and Latin
    acronyms rewritten as Devanagari words. Text that has none of these is
    returned unchanged (identity, not just equal)."""
    if not text:
        return text
    if not re.search(r"[0-9०-९%₹()\[\]{}/A-Za-z°]", text):
        return text

    out = text.translate(_DEVANAGARI_DIGITS)
    out = _RANGE.sub(lambda m: f"{number_to_hindi(int(m.group(1)))} से {number_to_hindi(int(m.group(2)))}", out)
    out = _PERCENT.sub(lambda m: f"{_spoken_number(m.group(1))} प्रतिशत", out)
    out = _CURRENCY.sub(lambda m: f"{_spoken_number(m.group(1))} रुपये", out)
    out = _WITH_UNIT.sub(lambda m: f"{_spoken_number(m.group(1))} {_UNITS[m.group(2).lower()]}", out)
    out = _PLAIN_NUMBER.sub(lambda m: _spoken_number(m.group(1)), out)
    out = _ACRONYM.sub(_spell, out)
    out = _LATIN_WORD.sub(lambda m: _GLOSSARY.get(m.group(0), m.group(0)), out)
    out = out.replace("&", " और ")
    out = _SLASH.sub(" ", out)
    # A bracketed aside is spoken as a short pause, not as the bracket itself.
    out = _BRACKETS.sub(", ", out)
    out = re.sub(r",\s*([,।?!.])", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip() if out != text else text


def unspoken_latin(text: str) -> list[str]:
    """Latin words still in `text` after normalisation - candidates for the
    glossary, since the voice may skip or mangle them."""
    return sorted(set(_LATIN_WORD.findall(hindi_tts_text(text))))
