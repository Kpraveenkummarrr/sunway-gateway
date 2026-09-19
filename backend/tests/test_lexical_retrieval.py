"""Lexical retrieval that understands how Hindi is written and spoken.

Background: retrieval used to rank by vector similarity, which is noise for
placeholder embeddings, and its "lexical fallback" searched only the 50 chunks
that noise ranked first - so the chunk answering a Lumpy Skin Disease question
was missing from the candidates about 40% of the time, and the caller heard the
fixed "information unavailable" reply. These tests need no database: they build
the index in memory from a small Hindi knowledge base.
"""

import json
import uuid

import pytest

from app.services.lexical_retrieval import (
    BUILTIN_SYNONYM_GROUPS,
    ChunkRecord,
    LexicalIndex,
    advice_terms,
    canonical,
    content_terms,
    load_synonym_groups,
    names_a_topic,
    stem,
    topic_words,
)
from tests.kb_fixtures import ANSWERABLE, LSD_KB, UNANSWERABLE

DOC = uuid.uuid4()


def _index(kb=LSD_KB, **kwargs) -> tuple[LexicalIndex, dict]:
    records, keys = [], {}
    for i, (key, text) in enumerate(kb):
        record = ChunkRecord(uuid.uuid4(), DOC, "kb.pdf", 1, i, text)
        records.append(record)
        keys[record.chunk_id] = key
    return LexicalIndex(records, **kwargs), keys


def _keys(index_and_keys, query: str, top_k: int = 4, **kwargs) -> list[str]:
    index, keys = index_and_keys
    return [keys[hit.record.chunk_id] for hit in index.search(query, top_k=top_k, **kwargs)]


# ---- spelling ----


@pytest.mark.parametrize(
    "variants",
    [
        ("लम्पी", "लंपी", "लम्पि", "लँपी"),  # conjunct / anusvara / short-long vowel / chandrabindu
        ("बुखार", "बुख़ार"),  # nukta
        ("गांठ", "गाँठ"),
        ("डॉक्टर", "डाक्टर"),
        ("LSD", "lsd"),
    ],
)
def test_spelling_variants_of_a_word_share_one_canonical_form(variants) -> None:
    assert len({canonical(v) for v in variants}) == 1, [canonical(v) for v in variants]


def test_devanagari_digits_are_read_as_digits_and_punctuation_is_dropped() -> None:
    assert canonical("५० मिलीलीटर, दो बार!") == "50 मिलिलिटर दो बार"


def test_zero_width_joiners_do_not_split_a_word() -> None:
    assert canonical("क्‍या") == canonical("क्या")


# ---- what counts as content ----


def test_function_words_are_not_content() -> None:
    terms = content_terms("लम्पी रोग के लक्षण क्या हैं?")
    assert not {"क्या", "के", "हैं"} & set(terms)
    assert len(terms) == 3


def test_romanised_and_english_function_words_are_not_content_either() -> None:
    assert content_terms("what is the treatment for lampi") == ["treatment", "lampi"]
    assert content_terms("iska ilaj kya hai") == ["ilaj"]


def test_inflected_forms_of_a_verb_meet() -> None:
    assert len(set(content_terms("फैलता फैलती फैलते"))) == 1


def test_latin_words_are_left_unstemmed() -> None:
    assert stem("lumpy") == "lumpy"
    assert stem("बचाव") == "बचाव"  # never below three characters


# ---- follow-up helpers ----


def test_a_question_that_names_the_subject_does_not_need_an_earlier_one() -> None:
    assert names_a_topic("लम्पी रोग का इलाज")
    assert names_a_topic("lumpy skin disease")
    assert names_a_topic("एलएसडी के लक्षण")
    assert not names_a_topic("इसका इलाज क्या है?")
    assert not names_a_topic("और बचाव?")


def test_the_subject_is_carried_in_the_callers_own_spelling() -> None:
    assert topic_words("लम्पी रोग के लक्षण क्या हैं?") == ["लम्पी", "रोग"]
    assert topic_words("Lampi skin disease ke lakshan?") == ["Lampi", "skin", "disease"]
    assert topic_words("इसका इलाज क्या है") == []


def test_advice_phrasing_is_recognised() -> None:
    assert advice_terms("मेरी गाय को गांठें हो गई हैं क्या करूं")
    assert advice_terms("lampi ho gaya to kya karein")
    assert advice_terms("what should I do")
    assert not advice_terms("लम्पी रोग के लक्षण क्या हैं")


# ---- retrieval ----


@pytest.mark.parametrize("question, _previous, expected", ANSWERABLE, ids=[q for q, _, _ in ANSWERABLE])
def test_a_question_reaches_the_passage_that_answers_it(question, _previous, expected) -> None:
    """Direct questions only: follow-ups are resolved by build_retrieval_query
    (see the database-backed tests). Whichever of several relevant passages ranks
    first is a job for the embedding model; the contract here is recall - the
    answering passage is among what the model is shown."""
    from app.services.knowledge_search import build_retrieval_query

    query = build_retrieval_query(question, _previous)
    assert expected in _keys(_index(), query, top_k=4), f"{question!r} (searched as {query!r})"


@pytest.mark.parametrize("question", UNANSWERABLE)
def test_a_question_the_knowledge_base_does_not_answer_retrieves_nothing(question) -> None:
    """The caller must be told honestly that there is no information - not
    handed a loosely related paragraph to paraphrase."""
    assert _keys(_index(), question) == []


def test_an_empty_or_function_word_only_query_retrieves_nothing() -> None:
    index = _index()
    assert _keys(index, "") == []
    assert _keys(index, "क्या है") == []


def test_an_empty_knowledge_base_retrieves_nothing() -> None:
    assert LexicalIndex([]).search("लम्पी रोग") == []


def test_the_result_is_bounded_and_best_first() -> None:
    index, keys = _index()
    hits = index.search("लम्पी रोग के लक्षण", top_k=2)
    assert len(hits) <= 2
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    assert all(0.0 < h.coverage <= 1.0 for h in hits)


def test_a_misspelt_word_is_matched_to_its_nearest_spelling_in_the_corpus() -> None:
    """ASR rarely writes a word the way the document does."""
    assert "symptoms" in _keys(_index(), "लम्पी रोग के लक्षन")  # लक्षन for लक्षण
    assert "spread" in _keys(_index(), "लम्पी रोग का संक्रमन कैसे होता है", top_k=8)  # संक्रमन for संक्रमण


def test_a_word_from_another_domain_does_not_make_an_unrelated_chunk_qualify() -> None:
    index = _index()
    # "थन" appears only in the mastitis passage; the lumpy question must not drag it in.
    assert "mastitis" not in _keys(index, "लम्पी रोग के लक्षण क्या हैं")


def test_a_stricter_coverage_requirement_returns_fewer_chunks() -> None:
    index = _index()
    loose = index[0].search("लम्पी रोग बचाव टीका", top_k=8, min_coverage=0.1)
    strict = index[0].search("लम्पी रोग बचाव टीका", top_k=8, min_coverage=0.9)
    assert len(strict) < len(loose)


# ---- editable synonyms ----


def test_the_synonym_groups_can_be_extended_from_a_json_file(tmp_path) -> None:
    kb = [("a", "पान के पत्ते और काली मिर्च का उल्लेख है"), ("b", "मक्खी और मच्छर से बचाव करें")]
    without = _index(kb)
    assert _keys(without, "tambul ke patte") == []
    path = tmp_path / "syn.json"
    path.write_text(json.dumps([["पान", "tambul", "betel"]]), encoding="utf-8")
    assert _keys(_index(kb, synonym_groups=load_synonym_groups(str(path))), "tambul ke patte") == ["a"]


def test_the_synonym_file_may_use_the_groups_key() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "syn.json"
        path.write_text(json.dumps({"groups": [["पान", "betel"]]}), encoding="utf-8")
        assert ("पान", "betel") in load_synonym_groups(str(path))


@pytest.mark.parametrize("content", ["not json at all", '{"groups": 5}', "[[\"one-word-group\"]]", ""])
def test_a_broken_synonym_file_never_breaks_retrieval(tmp_path, content) -> None:
    path = tmp_path / "syn.json"
    path.write_text(content, encoding="utf-8")
    assert load_synonym_groups(str(path)) == BUILTIN_SYNONYM_GROUPS


def test_a_missing_synonym_file_is_ignored() -> None:
    assert load_synonym_groups("/nonexistent/synonyms.json") == BUILTIN_SYNONYM_GROUPS
    assert load_synonym_groups(None) == BUILTIN_SYNONYM_GROUPS
