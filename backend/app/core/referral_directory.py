"""District → diagnostic-centre directory used for helpline escalation.

The client's requirement is blunt: the agent must never invent or guess a
centre name, address or phone number. A language model asked to recall
district centres will happily produce plausible ones, so the centres are not
left to the model at all — they are loaded from a file the operator controls
and injected verbatim, together with an instruction that forbids naming
anything that is not in the list. An empty or missing file is a safe state:
the agent is then told to name no centre at all.

File format (`REFERRAL_DIRECTORY_PATH`), either a bare list or an object
with a "centres" key:

    {
      "centres": [
        {
          "district": "Hisar",
          "name": "Disease Diagnostic Laboratory, LUVAS Hisar",
          "phone": "01662-000000",
          "address": "LUVAS campus, Hisar",
          "aliases": ["हिसार", "Hissar"]
        }
      ]
    }

Only "district" and "name" are required.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_PUNCTUATION = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+", flags=re.UNICODE)


@dataclass(frozen=True)
class ReferralCentre:
    district: str
    name: str
    phone: str = ""
    address: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def spoken_line(self) -> str:
        parts = [f"{self.district}: {self.name}"]
        if self.address:
            parts.append(self.address)
        if self.phone:
            parts.append(f"phone {self.phone}")
        return ", ".join(parts)


def normalise(text: str) -> str:
    """Casefold, drop punctuation, collapse whitespace. Devanagari survives
    unchanged — it has no case and is word characters to `re`."""
    return _WHITESPACE.sub(" ", _PUNCTUATION.sub(" ", (text or "").casefold())).strip()


_cache: dict[str, tuple[tuple[int, int], list[ReferralCentre]]] = {}


def load_referral_directory(path: str | None) -> list[ReferralCentre]:
    """Centres from `path`, or [] when unset, missing or unreadable.

    A malformed directory must never fail a live call, so every error is
    logged and treated as "no centres configured" — which makes the agent
    refuse to name one rather than fall back on the model's own guesses.
    """
    if not path or not str(path).strip():
        return []
    file_path = Path(path)
    try:
        stat = file_path.stat()
    except OSError as exc:
        logger.warning("referral directory %s is not readable: %s", path, exc)
        return []

    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _cache.get(str(file_path))
    if cached and cached[0] == stamp:
        return cached[1]

    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("referral directory %s could not be parsed: %s", path, exc)
        return []

    entries = raw.get("centres", []) if isinstance(raw, dict) else raw
    centres: list[ReferralCentre] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            district = str(entry.get("district", "")).strip()
            name = str(entry.get("name", "")).strip()
            if not district or not name:
                continue
            aliases = entry.get("aliases") or []
            centres.append(
                ReferralCentre(
                    district=district,
                    name=name,
                    phone=str(entry.get("phone", "")).strip(),
                    address=str(entry.get("address", "")).strip(),
                    aliases=tuple(str(a).strip() for a in aliases if str(a).strip()),
                )
            )
    if not centres:
        logger.warning("referral directory %s contains no usable centres", path)

    _cache[str(file_path)] = (stamp, centres)
    return centres


def find_centre(centres: list[ReferralCentre], text: str) -> ReferralCentre | None:
    """The centre whose district (or alias) the caller named, if any."""
    haystack = normalise(text)
    if not haystack:
        return None
    for centre in centres:
        for candidate in (centre.district, *centre.aliases):
            needle = normalise(candidate)
            if needle and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack):
                return centre
    return None


_NO_CENTRES_RULE = (
    "REFERRAL DIRECTORY: no diagnostic centre is configured on this helpline. "
    "Do not name any diagnostic centre, laboratory, officer or phone number. "
    "Refer the caller to their nearest government veterinary hospital or "
    "dispensary, or to LUVAS Hisar."
)


def directory_prompt_block(
    centres: list[ReferralCentre], *, caller_text: str | None = None
) -> str:
    """The REFERRAL DIRECTORY section appended to the system prompt."""
    if not centres:
        return _NO_CENTRES_RULE

    lines = "\n".join(f"- {centre.spoken_line()}" for centre in centres)
    block = (
        "REFERRAL DIRECTORY (the only centres you may name):\n"
        f"{lines}\n"
        "Name a centre only if its district appears in this list, and give it "
        "exactly as written. If the caller's district is not listed, say you "
        "do not have a centre listed for that district and refer them to their "
        "nearest government veterinary hospital or to LUVAS Hisar. Never "
        "invent or guess a centre, address or phone number."
    )
    matched = find_centre(centres, caller_text or "")
    if matched:
        block += f"\nThe caller appears to have named {matched.district}; its listed centre is above."
    return block
