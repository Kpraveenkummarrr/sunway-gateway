"""Deterministic Lumpy Skin Disease retrieval regressions.

The client's complaint was that the agent answers without reading the
knowledge base. These lock the retrieval half of that path: a question a
farmer would actually ask — in Hindi, Hinglish, transliterated, or as a bare
follow-up — must reach the Lumpy Skin Disease source and must not drag in an
unrelated document.

The embeddings here are the deterministic mock provider, so what these
exercise is the lexical/alias recovery and the follow-up query construction,
which is exactly the part that has to work when a cross-lingual vector is
weak. Semantic ranking quality needs the real embedding provider and is
measured on the client machine.
"""

import tempfile
from pathlib import Path

import pytest

from app.models.ai import AIMessage
from app.providers.embeddings.mock import MockEmbeddingProvider
from app.providers.llm.base import LLMProvider, LLMResponse
from app.services.conversation import create_session, handle_text_turn
from app.services.knowledge_ingestion import ingest_pdf
from app.services.knowledge_search import build_retrieval_query, search_chunks
from app.services.pdf_extraction import PyPDFTextExtractor
from tests.pdf_fixtures import make_pdf
from tests.test_helpline_persona import _settings

LSD_PAGES = [
    "Lumpy Skin Disease is a viral disease of cattle and buffalo spread by "
    "biting flies, mosquitoes and ticks.",
    "Signs of Lumpy Skin Disease include fever, firm nodules on the skin, "
    "swollen legs, watering eyes and a drop in milk yield.",
    "Prevention of Lumpy Skin Disease: vaccinate healthy animals, keep an "
    "affected animal separate, and control flies and ticks in the shed.",
    "Ethnoveterinary preparation for Lumpy Skin Disease described by Sampurna "
    "Nand Yadav and colleagues and published by NDDB uses betel leaves, "
    "black pepper, common salt and jaggery.",
    "Milk from an animal with Lumpy Skin Disease should be boiled before use, "
    "and meat must only come from animals passed by a veterinary inspector.",
]

# The client's mandatory question list. Each pair is a question a farmer asks
# and a word that must appear in what the AI is given to answer from.
MANDATORY_QUESTIONS = [
    ("What is Lumpy Skin Disease?", "viral disease"),
    ("लम्पी रोग के लक्षण क्या हैं?", "nodules"),
    ("lumpy skin disease kaise failta hai?", "flies"),
    ("लम्पी रोग से बचाव कैसे करें?", "Prevention"),
    ("lampi vaccine kab lagwaye", "vaccinate"),
    ("क्या दूध पीना सुरक्षित है lumpy me?", "Milk"),
    ("lampi", "Lumpy Skin Disease"),
]
UNRELATED_PAGE = (
    "Mastitis is an udder infection. Clean the udder before milking and keep "
    "the milking area dry."
)

FARMER_QUERIES = [
    "What is Lumpy Skin Disease?",
    "लम्पी स्किन डिजीज क्या है?",
    "लम्पी रोग के लक्षण क्या हैं?",
    "Lumpy skin disease ke lakshan kya hain?",
    "lampi skin disease se kaise bachaye",
    "lampy skin disease vaccine",
    "lampi",
]


class _FixedLLM(LLMProvider):
    def __init__(self) -> None:
        self.contexts: list[str | None] = []

    async def generate_response(self, *, system_prompt, history, retrieved_context) -> LLMResponse:
        self.contexts.append(retrieved_context)
        return LLMResponse(text="जी, बताती हूँ।")


@pytest.fixture
async def lsd_corpus(db_session):
    """An LSD source plus one unrelated source, so a hit has to be a real hit."""
    provider = MockEmbeddingProvider(dimensions=1536)
    with tempfile.TemporaryDirectory() as tmp:
        lsd = await ingest_pdf(
            db_session,
            file_bytes=make_pdf(LSD_PAGES),
            original_filename="lumpy-skin-disease-luvas.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=provider,
            chunk_size=400,
            chunk_overlap=50,
        )
        other = await ingest_pdf(
            db_session,
            file_bytes=make_pdf([UNRELATED_PAGE]),
            original_filename="mastitis.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=provider,
            chunk_size=400,
            chunk_overlap=50,
        )
        try:
            yield provider, lsd, other
        finally:
            await db_session.delete(lsd)
            await db_session.delete(other)
            await db_session.commit()


async def _search(db_session, provider, query):
    return await search_chunks(
        db_session,
        query_embedding=await provider.embed_one(query),
        query_text=query,
        top_k=4,
        similarity_threshold=0.75,
        embedding_space=provider.embedding_space,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("query", FARMER_QUERIES)
async def test_a_farmer_question_reaches_the_lumpy_skin_source(lsd_corpus, db_session, query) -> None:
    provider, lsd, other = lsd_corpus
    results = await _search(db_session, provider, query)

    assert results, f"no chunk retrieved for {query!r}"
    assert {r.document_id for r in results} == {lsd.id}
    assert other.id not in {r.document_id for r in results}


@pytest.mark.asyncio
@pytest.mark.parametrize("query, expected_keyword", MANDATORY_QUESTIONS, ids=[q for q, _ in MANDATORY_QUESTIONS])
async def test_every_mandatory_question_reaches_the_knowledge_that_answers_it(
    lsd_corpus, db_session, query, expected_keyword
) -> None:
    """Recall, not ranking: the mock embeddings carry no meaning, so what is
    asserted is that the answering passage reaches the model's context at all.
    Which of several relevant passages ranks first needs the real embedding
    provider and is measured on the client machine."""
    provider, lsd, other = lsd_corpus
    results = await search_chunks(
        db_session,
        query_embedding=await provider.embed_one(query),
        query_text=query,
        top_k=10,
        similarity_threshold=0.75,
        embedding_space=provider.embedding_space,
    )

    assert results, f"no knowledge retrieved for {query!r}"
    assert {r.document_id for r in results} == {lsd.id}
    context = " ".join(r.chunk_text for r in results)
    assert expected_keyword.lower() in context.lower(), (
        f"{query!r} retrieved knowledge that does not contain {expected_keyword!r}"
    )


@pytest.mark.asyncio
async def test_an_unrelated_question_does_not_pull_in_the_lumpy_source(lsd_corpus, db_session) -> None:
    provider, lsd, _ = lsd_corpus
    results = await _search(db_session, provider, "How do I keep the udder clean during milking?")

    assert lsd.id not in {r.document_id for r in results}


@pytest.mark.asyncio
async def test_the_ethnoveterinary_attribution_is_retrievable(lsd_corpus, db_session) -> None:
    provider, lsd, _ = lsd_corpus
    results = await _search(db_session, provider, "lampi ke liye gharelu nuskha")

    text = " ".join(r.chunk_text for r in results)
    assert "Sampurna" in text and "NDDB" in text


# ---- follow-up questions ----


def test_a_bare_follow_up_borrows_the_previous_question_topic() -> None:
    query = build_retrieval_query("iska ilaj kya hai?", ["lampi skin disease ke lakshan"])
    assert "lampi skin disease" in query
    assert "iska ilaj" in query


def test_a_self_contained_question_is_not_rewritten() -> None:
    question = "Lumpy skin disease se bachne ke liye kya karein?"
    assert build_retrieval_query(question, ["something earlier"]) == question


def test_a_follow_up_with_no_earlier_turn_is_left_alone() -> None:
    assert build_retrieval_query("iska ilaj kya hai?", []) == "iska ilaj kya hai?"


def test_a_repeated_question_is_not_doubled() -> None:
    assert build_retrieval_query("lampi", ["lampi"]) == "lampi"


@pytest.mark.asyncio
async def test_a_follow_up_turn_still_retrieves_the_lumpy_source(lsd_corpus, db_session) -> None:
    """The regression behind "the AI does not read the database": the second,
    pronoun-only turn used to retrieve nothing at all."""
    provider, lsd, _ = lsd_corpus
    settings = _settings(rag_similarity_threshold=0.75, rag_top_k=4)
    llm = _FixedLLM()
    session = await create_session(db_session, language="hi")
    try:
        first = await handle_text_turn(
            db_session,
            session,
            "लम्पी रोग के लक्षण क्या हैं?",
            settings=settings,
            embedding_provider=provider,
            llm_provider=llm,
        )
        assert {c.document_id for c in first.retrieved_chunks} == {lsd.id}

        follow_up = await handle_text_turn(
            db_session,
            session,
            "iska ilaj kya hai?",
            settings=settings,
            embedding_provider=provider,
            llm_provider=llm,
        )
        assert follow_up.retrieved_chunks, "follow-up turn retrieved nothing"
        assert {c.document_id for c in follow_up.retrieved_chunks} == {lsd.id}
        assert llm.contexts[1]
    finally:
        from sqlalchemy import delete

        await db_session.execute(delete(AIMessage).where(AIMessage.session_id == session.id))
        await db_session.delete(session)
        await db_session.commit()


@pytest.mark.asyncio
async def test_the_follow_up_text_on_its_own_retrieves_nothing(lsd_corpus, db_session) -> None:
    """Why the carry-over exists at all: the pronoun-only utterance names no
    topic, so retrieval on it alone comes back empty."""
    provider, _, _ = lsd_corpus
    assert await _search(db_session, provider, "iska ilaj kya hai?") == []
