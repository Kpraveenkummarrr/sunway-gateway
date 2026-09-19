"""Full-corpus lexical retrieval that understands Hindi spelling.

Why this exists
---------------
Retrieval used to depend on vector similarity. With placeholder (`mock`) or weak
embeddings that similarity is noise, and the "lexical" fallback was a SQL
`LIKE` over *candidates ordered by that noise and cut to 50* - so on a
123-chunk knowledge base the chunk that answers the question was simply not in
the candidate set roughly 40% of the time. The reply was then a fixed
"information unavailable" sentence, whatever the document actually said.

This searches every ready chunk in the current embedding space, so it cannot
be starved by a bad vector while still excluding documents whose embedding
provenance is stale or unknown. It is an ordinary BM25 ranking made robust to
how Hindi is actually written and spoken:

  * Devanagari is canonicalised before matching, on both sides: nukta and
    chandrabindu folded, nasal conjuncts written as anusvara (लम्पी = लंपी),
    long/short i-u vowels folded (लम्पी = लम्पि), Devanagari digits read as digits;
  * function words in Hindi, Romanised Hindi and English are dropped, so
    "क्या", "है", "के" no longer look like content;
  * inflections are merged by light suffix stripping (फैलता/फैलती/फैलना);
  * domain synonyms are expanded (लम्पी / लंपी / गांठदार / lumpy / lampi), from
    built-in groups plus an optional editable JSON file;
  * a query word absent from the corpus is matched to its nearest spelling by
    character-trigram similarity (ASR rarely spells a word the way the PDF does).

A chunk is returned only if it covers enough of the question (IDF-weighted), so
a question the knowledge base does not answer still returns nothing and the
caller is told so honestly rather than handed a loosely related paragraph.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

# ------------------------------------------------------------------ canonical form

_NUKTA = "़"
_HALANT = "्"
_CHANDRABINDU = "ँ"
_ANUSVARA = "ं"
_NASAL_CONJUNCT = re.compile("[ङञणनम]" + _HALANT + "(?=[क-ह])")
_FOLD = {
    ord("ी"): "ि",  # ी -> ि
    ord("ू"): "ु",  # ू -> ु
    ord("ई"): "इ",  # ई -> इ
    ord("ऊ"): "उ",  # ऊ -> उ
    ord("ॉ"): "ा",  # ॉ -> ा
    ord("ऑ"): "आ",  # ऑ -> आ
    ord("‍"): None,  # zero-width joiner
    ord("‌"): None,  # zero-width non-joiner
}
_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def canonical(text: str) -> str:
    """Spelling-insensitive form of `text` for matching (not for display)."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = t.replace(_NUKTA, "").replace(_CHANDRABINDU, _ANUSVARA)
    t = _NASAL_CONJUNCT.sub(_ANUSVARA, t)
    t = t.translate(_FOLD).translate(_DIGITS)
    chars = [c if (c.isspace() or unicodedata.category(c)[0] in ("L", "M", "N")) else " " for c in t]
    return " ".join("".join(chars).split())


# ------------------------------------------------------------------ stop words

_STOPWORDS_RAW = """
के का की को में मे से पर और या कि यह ये वह वो इस उस इन उन एक है हैं हूँ हूं था थी थे हो होता होती होते होगा होगी
कर करे करें करना करता करती करते किया क्या कैसे कब कहाँ कहां क्यों कौन कितना कितनी कितने बारे बताइए बताइये बताओ
बताएं बताएँ बताना मुझे मेरे मेरा मेरी हमें हमारे आप आपके आपका आपकी तो ही भी नहीं नही जो जब तब लिए लिये द्वारा
सकता सकती सकते चाहिए चाहिये वाला वाली वाले कुछ सब अगर तक बहुत इसका इसकी इसके उसका उसकी उसके इसमें उसमें इससे
उससे इसलिए क्योंकि लेकिन परंतु पर्यंत रहा रही रहे गया गई गये गए जाता जाती जाते जाए जाएं दें दे दिया देना लें ले
लिया लेना अब यहां यहाँ वहां वहाँ कोई किसी कैसा कैसी कैसे क्या करूं करूँ करू करुं हुआ हुई हुए होना होने रहता
रहती रहते पड़ा पड़ी पड़े लगा लगी लगे सकूं सकूँ चाहता चाहती चाहते जी हाँ हां अच्छा ठीक
तरीके तरीका तरीकों उपाय उपायों जानकारी बात बातें बताइएगा सलाह
a an and are about can do does for give how is ke kya me the tell what with you hai hain batao mujhe mein par
ka ki ko se hoga hota hoti kaise kab kahan kyun kaun kitna kitni kitne aur iska iski uska uski ye yeh woh wo is us ne
tha thi the kar karo karein karen kare karna karu karun bataiye batayein liye lie tak bahut lekin ya to bhi nahi nhi koi kisi
hua hui hue gaya gayi kaisa kaisi ho
please tell me my your this that these those there here it its of in on at to from by as be was were will would should
could i we he she they them us our their
"""

# ------------------------------------------------------------------ stemming

_SUFFIXES = sorted(
    [
        "ियों", "ुओं", "ाओं", "ाएंगे", "ाएंगी", "ाएगा", "ाएगी", "ाएं", "ाना", "ाता", "ाति", "ाते",
        "ता", "ति", "ते", "ना", "ने", "ों", "ें", "ओं", "ंगे", "ंगी", "ेगा", "ेगी", "ां", "ीं", "ा", "े", "ो", "ि", "ं",
    ],
    key=len,
    reverse=True,
)
_DEVANAGARI = re.compile("[ऀ-ॿ]")


def stem(token: str) -> str:
    """Strips one common Hindi inflection so फैलता/फैलती/फैलना meet. Only
    Devanagari tokens, and never below three characters."""
    if not _DEVANAGARI.search(token):
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[: -len(suffix)]
    return token


STOPWORDS: frozenset[str] = frozenset(canonical(w) for w in _STOPWORDS_RAW.split())


def content_terms(text: str) -> list[str]:
    """The meaningful, stemmed words of `text` (repeats kept)."""
    return [
        stem(tok)
        for tok in canonical(text).split()
        if len(tok) > 1 and tok not in STOPWORDS
    ]


# ------------------------------------------------------------------ synonyms

# Domain synonym groups: every member stands for every other. Built in for the
# Lumpy Skin Disease helpline; extend or replace through RETRIEVAL_SYNONYMS_PATH
# (a JSON list of lists, editable by staff) - no code change needed.
BUILTIN_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("लम्पी", "लंपी", "गांठदार", "गाँठदार", "गांठों", "lumpy", "lampi", "lampy", "lsd", "एलएसडी", "ganthdar"),
    ("लक्षण", "लक्षणों", "चिन्ह", "निशान", "पहचान", "symptom", "symptoms", "signs", "lakshan"),
    ("फैलता", "फैलना", "फैलाव", "संक्रमण", "प्रसार", "फैलती", "फैलते", "spread", "spreads", "failta", "transmission"),
    ("बचाव", "रोकथाम", "बचना", "सुरक्षा", "prevention", "prevent", "bachav", "बचाएं", "बचें", "बचे", "बचाने",
     "बचाना", "बचाओ", "बचाइए", "बचाऊं", "bachen", "bachne", "bachaav", "bachao"),
    ("टीका", "टीके", "टीकाकरण", "वैक्सीन", "वैक्सिन", "टीकों", "vaccine", "vaccination", "tika", "tikakaran"),
    ("इलाज", "उपचार", "दवा", "दवाई", "चिकित्सा", "ilaj", "treatment", "upchar", "medicine"),
    ("दूध", "दुग्ध", "milk", "doodh", "dudh"),
    ("मांस", "मीट", "गोश्त", "meat", "mans"),
    ("बुखार", "ज्वर", "बुख़ार", "fever", "bukhar"),
    ("गांठ", "गांठें", "गाँठ", "गठान", "नोड्यूल", "nodule", "nodules", "lump", "lumps", "ganth"),
    ("मक्खी", "मक्खियों", "मक्खियां", "मच्छर", "किलनी", "makkhi", "flies", "mosquito", "ticks"),
    ("रोग", "रोगों", "बीमारी", "बिमारी", "बीमारियों", "rog", "bimari", "disease", "diseases"),
    ("त्वचा", "चमड़ी", "खाल", "skin", "twacha", "tvacha"),
    ("गाय", "गौ", "गायों", "cow", "cattle", "gaay"),
    ("भैंस", "भैंसों", "buffalo", "bhains"),
    ("पशु", "पशुओं", "जानवर", "मवेशी", "animal", "pashu"),
    ("डॉक्टर", "चिकित्सक", "पशुचिकित्सक", "वेट", "डाक्टर", "vet", "doctor", "veterinarian"),
)


def load_synonym_groups(path: str | None) -> tuple[tuple[str, ...], ...]:
    """Built-in groups plus those in the JSON file at `path` (if any). A missing
    or malformed file is ignored: retrieval must keep working."""
    groups = list(BUILTIN_SYNONYM_GROUPS)
    if path and str(path).strip():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            entries = raw.get("groups", raw) if isinstance(raw, dict) else raw
            extra = [
                tuple(str(word) for word in entry)
                for entry in entries
                if isinstance(entry, list) and len(entry) > 1
            ]
        except (OSError, ValueError, TypeError):
            return tuple(groups)  # unreadable or malformed: the built-in groups still work
        groups.extend(extra)
    return tuple(groups)


def _synonym_map(groups) -> dict[str, frozenset[str]]:
    mapping: dict[str, set[str]] = defaultdict(set)
    for group in groups:
        stems = {stem(canonical(word)) for word in group if canonical(word)}
        stems = {s for s in stems if len(s) > 1}
        for s in stems:
            mapping[s] |= stems - {s}
    return {k: frozenset(v) for k, v in mapping.items()}


# Phrasing that asks WHAT TO DO rather than naming a fact. The passage that
# answers it says "keep the animal apart, call the veterinary hospital" and
# shares no word with "क्या करूं", so those words are added to the search (they
# help rank a chunk; they are not required for it to qualify).
_ADVICE_QUESTION = re.compile(
    r"(क्या\s+(करुं|करें|करे|करना|करू|कर)\b|kya\s+kar(u|un|oon|ein|en|e|na)\b|what\s+(should\s+i\s+do|to\s+do))"
)
_ADVICE_TERMS = ("उपचार", "चिकित्सक", "अलग", "सलाह", "सूचना")


_EDGE_PUNCTUATION = "?!.,;:।॥'\"()[]{}-–—…“”‘’"


def advice_terms(query: str) -> tuple[str, ...]:
    return _ADVICE_TERMS if _ADVICE_QUESTION.search(canonical(query)) else ()


def names_a_topic(text: str, groups=BUILTIN_SYNONYM_GROUPS) -> bool:
    """Does `text` name the helpline's subject (Lumpy Skin Disease) itself, so
    it does not depend on an earlier question for its topic?"""
    subject = {stem(canonical(word)) for word in groups[0]}
    return any(term in subject for term in content_terms(text))


def topic_words(text: str, groups=BUILTIN_SYNONYM_GROUPS) -> list[str]:
    """The words of `text` that name the helpline's subject, as the caller
    spelled them. Used to carry the subject into a follow-up that does not
    repeat it; the original spelling (not the folded matching form) is kept
    because the text is also embedded, and an embedding model reads लम्पी better
    than लंपि."""
    # Preserve the whole disease phrase ("lumpy skin disease" / "लम्पी
    # रोग"), not only the distinctive first word.  The first group marks
    # Lumpy itself; the disease and skin groups retain its natural qualifiers.
    subject = {
        stem(canonical(word))
        for group in (groups[0], groups[11], groups[12])
        for word in group
    }
    words: list[str] = []
    for raw in (text or "").split():
        word = raw.strip(_EDGE_PUNCTUATION)
        folded = canonical(word)
        if folded and " " not in folded and folded not in STOPWORDS and stem(folded) in subject:
            words.append(word)
    return words


# ------------------------------------------------------------------ index


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: UUID
    document_id: UUID
    filename: str
    page_number: int | None
    chunk_index: int
    text: str


@dataclass(frozen=True)
class LexicalHit:
    record: ChunkRecord
    score: float  # BM25
    coverage: float  # 0..1, IDF-weighted share of the question this chunk covers
    matched: tuple[str, ...] = field(default_factory=tuple)


def _trigrams(term: str) -> frozenset[str]:
    padded = f"#{term}#"
    return frozenset(padded[i : i + 3] for i in range(len(padded) - 2))


class LexicalIndex:
    K1 = 1.2
    B = 0.75
    SYNONYM_WEIGHT = 0.8
    FUZZY_MIN_SIMILARITY = 0.55
    UNSEEN_WEIGHT = 0.6
    INTENT_WEIGHT = 0.5

    def __init__(self, records: list[ChunkRecord], synonym_groups=BUILTIN_SYNONYM_GROUPS) -> None:
        self.records = records
        self._synonyms = _synonym_map(synonym_groups)
        self._lengths: list[int] = []
        self._postings: dict[str, dict[int, int]] = defaultdict(dict)
        for index, record in enumerate(records):
            terms = content_terms(record.text)
            self._lengths.append(len(terms))
            for term, count in Counter(terms).items():
                self._postings[term][index] = count
        self._n = len(records)
        self._avg_length = (sum(self._lengths) / self._n) if self._n else 0.0
        self._grams = {term: _trigrams(term) for term in self._postings}

    def idf(self, term: str) -> float:
        df = len(self._postings.get(term, ()))
        return math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))

    @property
    def unseen_idf(self) -> float:
        return math.log(1.0 + (self._n + 0.5) / 0.5)

    def _expand(self, term: str) -> dict[str, float]:
        """The corpus terms that stand in for query `term`, with weights."""
        found: dict[str, float] = {}
        if term in self._postings:
            found[term] = 1.0
        for synonym in self._synonyms.get(term, ()):
            if synonym in self._postings:
                found.setdefault(synonym, self.SYNONYM_WEIGHT)
        if not found and len(term) >= 3:
            grams = _trigrams(term)
            for candidate, candidate_grams in self._grams.items():
                if abs(len(candidate) - len(term)) > 3:
                    continue
                overlap = len(grams & candidate_grams)
                similarity = 2 * overlap / (len(grams) + len(candidate_grams))
                if similarity >= self.FUZZY_MIN_SIMILARITY:
                    found[candidate] = max(found.get(candidate, 0.0), 0.5 * similarity)
        return found

    def search(self, query: str, *, top_k: int = 4, min_coverage: float = 0.34) -> list[LexicalHit]:
        if not self._n:
            return []
        base_terms = list(dict.fromkeys(content_terms(query)))
        if not base_terms:
            return []
        expansions = {term: self._expand(term) for term in base_terms}
        # A question word that has no counterpart at all in the corpus counts
        # against coverage - but as a typical question word, not as the rarest
        # possible one: a caller's phrasing ("बचें") should not outweigh the
        # topic word the passage does contain. If NOTHING matches, no chunk can
        # cover anything and the search returns empty.
        found_idfs = [
            max(self.idf(v) for v in variants) for variants in expansions.values() if variants
        ]
        if not found_idfs:
            return []
        unseen = self.UNSEEN_WEIGHT * (sum(found_idfs) / len(found_idfs))
        base_idf = {
            term: (max(self.idf(v) for v in variants) if variants else unseen)
            for term, variants in expansions.items()
        }
        denominator = sum(base_idf.values())
        if denominator <= 0:
            return []

        scores: dict[int, float] = defaultdict(float)
        covered: dict[int, dict[str, float]] = defaultdict(dict)
        for word in advice_terms(query):
            term = stem(canonical(word))
            for index, tf in self._postings.get(term, {}).items():
                norm = 1 - self.B + self.B * (self._lengths[index] / self._avg_length if self._avg_length else 1.0)
                scores[index] += self.INTENT_WEIGHT * self.idf(term) * (tf * (self.K1 + 1)) / (tf + self.K1 * norm)
        for term, variants in expansions.items():
            for variant, weight in variants.items():
                idf = self.idf(variant)
                for index, tf in self._postings[variant].items():
                    norm = 1 - self.B + self.B * (self._lengths[index] / self._avg_length if self._avg_length else 1.0)
                    scores[index] += weight * idf * (tf * (self.K1 + 1)) / (tf + self.K1 * norm)
                    if weight > covered[index].get(term, 0.0):
                        covered[index][term] = weight

        hits: list[LexicalHit] = []
        for index, score in scores.items():
            coverage = sum(base_idf[t] * w for t, w in covered[index].items()) / denominator
            if coverage < min_coverage:
                continue
            hits.append(LexicalHit(self.records[index], score, min(1.0, coverage), tuple(sorted(covered[index]))))
        hits.sort(key=lambda h: (-h.score, h.record.chunk_index))
        return hits[:top_k]
