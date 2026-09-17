"""Turning model output into something a TTS engine can read aloud.

The system prompt tells the model not to use markdown, bullets or headings,
and mostly it complies — but "mostly" is not good enough on a phone line. A
stray `**` is read as "star star" by some engines and swallowed by others, a
line that starts `1.` turns a spoken answer into a document being recited,
and a newline is not a pause to a TTS engine, it is nothing at all.

This is the last thing that touches the text before synthesis. It removes
what cannot be spoken and keeps the sentence boundaries, because those are
what the engine turns into prosody.
"""

import re
import unicodedata

# Markdown emphasis/code markers around a word. Removing the marker keeps the
# word; removing the word would lose the answer.
_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3}|`{1,3})(?=\S)(.+?)(?<=\S)\1", flags=re.DOTALL)
_STRAY_MARKERS = re.compile(r"[*`_]{1,3}")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", flags=re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", flags=re.MULTILINE)
# A list marker only counts at the start of a line, so "2.5 litre" survives.
_BULLET = re.compile(r"^\s{0,4}[-*•·—]\s+", flags=re.MULTILINE)
_NUMBERED = re.compile(r"^\s{0,4}(\d{1,2}|[१२३४५६७८९०]{1,2})[.)]\s+", flags=re.MULTILINE)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", flags=re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_SENTENCE_END = "।॥?!."
_REPEATED_PUNCT = re.compile(r"([।॥?!.,])\1{1,}")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([।॥?!,.])")
_WHITESPACE = re.compile(r"[ \t]+")


def _is_speakable(char: str) -> bool:
    """Emoji, pictographs and box-drawing characters are not speech. Letters,
    digits, marks, whitespace and ordinary punctuation are."""
    if char in "\n\r\t ":
        return True
    category = unicodedata.category(char)
    if category[0] in ("L", "M", "N"):
        return True
    # Keep sentence punctuation and the few symbols that carry meaning aloud.
    return char in "।॥?!.,;:-–—()'\"%/&+₹"


def spoken_text(text: str) -> str:
    """The version of `text` that should be sent to the TTS engine."""
    if not text:
        return ""

    cleaned = _LINK.sub(r"\1", text)
    cleaned = _TABLE_ROW.sub(" ", cleaned)
    cleaned = _HEADING.sub("", cleaned)
    cleaned = _BLOCKQUOTE.sub("", cleaned)

    # Emphasis first (paired markers), then whatever markers are left over
    # from an unbalanced pair.
    for _ in range(3):
        cleaned, count = _EMPHASIS.subn(r"\2", cleaned)
        if not count:
            break
    cleaned = _STRAY_MARKERS.sub("", cleaned)

    # A list item is a sentence when it is spoken, so give it an ending
    # before the lines are joined — otherwise two items run together.
    lines = []
    for line in cleaned.splitlines():
        was_item = bool(_BULLET.match(line) or _NUMBERED.match(line))
        line = _BULLET.sub("", line)
        line = _NUMBERED.sub("", line)
        line = line.strip()
        if not line:
            continue
        if was_item and line[-1] not in _SENTENCE_END:
            line += "।"
        lines.append(line)

    cleaned = " ".join(lines)
    cleaned = "".join(char for char in cleaned if _is_speakable(char))
    cleaned = _REPEATED_PUNCT.sub(r"\1", cleaned)
    cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned
