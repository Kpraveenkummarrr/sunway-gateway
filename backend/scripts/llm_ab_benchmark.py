"""Gemini vs Sarvam-M (or any two LLM_PROVIDER values), same questions, same
retrieved context.

    cd backend && .venv/bin/python scripts/llm_ab_benchmark.py --out /var/tmp/sunway-llm-ab
    .venv/bin/python scripts/llm_ab_benchmark.py --provider-a mock --provider-b mock  # harness self-check, no keys/weights needed
    .venv/bin/python scripts/llm_ab_benchmark.py --provider-a gemini --provider-b sarvam_m --seed-demo-kb

Runs the client's 12 mandatory questions (Hindi greeting, LSD symptoms,
spread, prevention, vaccination, milk/meat safety, suspected case, a
same-topic follow-up, Hinglish, transliteration, a short follow-up, and an
unclear question) through two configured LLM providers.

RAG parity (critical for a valid comparison): retrieval runs ONCE per
question and the identical system prompt + context is handed to BOTH
providers directly — neither provider does its own retrieval, so there is
no way for the comparison to be contaminated by two different context sets.

Non-streaming honesty: neither provider implementation here streams tokens,
so "first response latency" and "complete response latency" are the same
number for both. That is reported as-is, not disguised as two different
measurements — see docs/LLM_AB_TEST.md for what that means for real-call
latency.

--seed-demo-kb temporarily ingests a small built-in Lumpy Skin Disease
reference (cleaned up at the end) so retrieval has something to find on a
machine with no knowledge base loaded — it does not touch or replace a real
uploaded document.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings, get_settings  # noqa: E402
from app.core.db import AsyncSessionLocal, engine  # noqa: E402
from app.providers.embeddings.factory import get_embedding_provider  # noqa: E402
from app.providers.llm.base import LLMMessage, LLMProviderError  # noqa: E402
from app.providers.llm.factory import get_llm_provider  # noqa: E402
from app.services.knowledge_ingestion import ingest_pdf  # noqa: E402
from app.services.knowledge_search import build_retrieval_query, search_chunks  # noqa: E402
from app.services.pdf_extraction import PyPDFTextExtractor  # noqa: E402
from app.services.rag_context import build_context  # noqa: E402
from app.services.spoken_text import spoken_text  # noqa: E402
from app.services.call_controller import speech_chunks  # noqa: E402
from tests.pdf_fixtures import make_pdf  # noqa: E402

DEMO_KB_PAGES = [
    "Lumpy Skin Disease is a viral disease of cattle and buffalo spread by biting flies, "
    "mosquitoes and ticks.",
    "Signs of Lumpy Skin Disease include fever, firm nodules on the skin, swollen legs, "
    "watering eyes and a drop in milk yield.",
    "Prevention of Lumpy Skin Disease: vaccinate healthy animals, keep an affected animal "
    "separate, and control flies and ticks in the shed.",
    "Milk from an animal with Lumpy Skin Disease should be boiled before use, and meat must "
    "only come from animals passed by a veterinary inspector.",
]

# Part 5's mandatory question list. `history` holds prior turns for the two
# follow-up cases (8 and 11), so the model actually has something to follow
# up on rather than answering a bare pronoun cold.
QUESTIONS: list[dict] = [
    {"id": "1-greeting", "text": "नमस्कार, कैसे हैं आप?", "history": []},
    {"id": "2-symptoms", "text": "लम्पी रोग के लक्षण क्या हैं?", "history": []},
    {"id": "3-spread", "text": "लम्पी रोग कैसे फैलता है?", "history": []},
    {"id": "4-prevention", "text": "लम्पी रोग से बचाव कैसे करें?", "history": []},
    {"id": "5-vaccination", "text": "टीका कब लगवाना चाहिए?", "history": []},
    {"id": "6-milk-meat-safety", "text": "क्या लम्पी रोग में दूध पीना सुरक्षित है?", "history": []},
    {"id": "7-suspected-case", "text": "मेरी गाय को शरीर पर गांठें हो गई हैं, क्या करूं?", "history": []},
    {
        "id": "8-followup-after-previous",
        "text": "इसका इलाज क्या है?",
        "history": [("user", "लम्पी रोग के लक्षण क्या हैं?"), ("assistant", "बुखार और त्वचा पर गांठें आम लक्षण हैं।")],
    },
    {"id": "9-hinglish", "text": "lumpy disease ke symptoms kya hain?", "history": []},
    {"id": "10-transliterated", "text": "lampi rog kaise failta hai", "history": []},
    {
        "id": "11-short-followup",
        "text": "कितने दिन में ठीक होगा?",
        "history": [("user", "लम्पी रोग से बचाव कैसे करें?"), ("assistant", "स्वस्थ पशुओं को टीका लगवाएं।")],
    },
    {"id": "12-unclear", "text": "वो वाला मामला क्या हुआ था?", "history": []},
]


@dataclass
class QuestionResult:
    question_id: str
    text: str
    retrieval_count: int
    document_ids: list[str]
    chunk_ids: list[str]
    latency_first_ms: dict[str, int | None]
    latency_complete_ms: dict[str, int | None]
    reply: dict[str, str | None]
    reply_chars: dict[str, int]
    spoken_sentences: dict[str, int]
    markup_leaked: dict[str, bool]
    error: dict[str, str | None]


async def _seed_demo_kb(db, embedding_provider) -> object:
    with __import__("tempfile").TemporaryDirectory() as tmp:
        document = await ingest_pdf(
            db,
            file_bytes=make_pdf(DEMO_KB_PAGES),
            original_filename="llm-ab-benchmark-demo-kb.pdf",
            storage_dir=Path(tmp),
            extractor=PyPDFTextExtractor(),
            embedding_provider=embedding_provider,
            chunk_size=400,
            chunk_overlap=50,
        )
    return document


async def _retrieve(db, embedding_provider, settings: Settings, text: str, history: list[tuple[str, str]]):
    previous_user_texts = [content for role, content in history if role == "user"]
    search_text = build_retrieval_query(text, previous_user_texts)
    query_embedding = await embedding_provider.embed_one(search_text)
    results = await search_chunks(
        db,
        query_embedding=query_embedding,
        query_text=search_text,
        top_k=settings.rag_top_k,
        similarity_threshold=settings.rag_similarity_threshold,
        embedding_space=embedding_provider.embedding_space,
    )
    context = build_context(results, max_chars=settings.ai_max_context_chars)
    return results, context


async def _call_provider(provider, *, system_prompt: str, history: list[tuple[str, str]], text: str, context):
    llm_history = [LLMMessage(role=role, content=content) for role, content in history]
    llm_history.append(LLMMessage(role="user", content=text))
    started = time.monotonic()
    response = await provider.generate_response(
        system_prompt=system_prompt, history=llm_history, retrieved_context=context
    )
    # Neither provider streams tokens, so "first" and "complete" are the
    # same measured latency here — see the module docstring.
    latency_ms = int((time.monotonic() - started) * 1000)
    return response, latency_ms


def _markup_leaked(text: str) -> bool:
    return spoken_text(text) != text and any(m in text for m in ("*", "#", "`", "|", "http"))


async def run(args) -> dict:
    settings = get_settings()
    embedding_provider = get_embedding_provider(settings)

    providers: dict[str, object] = {}
    construction_errors: dict[str, str] = {}
    for label, name in (("A", args.provider_a), ("B", args.provider_b)):
        try:
            per_provider_settings = settings.model_copy(update={"llm_provider": name})
            providers[label] = get_llm_provider(per_provider_settings)
        except LLMProviderError as exc:
            construction_errors[label] = str(exc)

    system_prompt = settings.system_prompt_for(settings.ai_language)
    seeded_document = None
    results: list[QuestionResult] = []

    async with AsyncSessionLocal() as db:
        if args.seed_demo_kb:
            seeded_document = await _seed_demo_kb(db, embedding_provider)

        try:
            for question in QUESTIONS:
                retrieved, context = await _retrieve(
                    db, embedding_provider, settings, question["text"], question["history"]
                )
                latency_first: dict[str, int | None] = {}
                latency_complete: dict[str, int | None] = {}
                reply: dict[str, str | None] = {}
                reply_chars: dict[str, int] = {}
                spoken_sentences: dict[str, int] = {}
                markup_leaked: dict[str, bool] = {}
                error: dict[str, str | None] = {}

                for label in ("A", "B"):
                    provider_name = args.provider_a if label == "A" else args.provider_b
                    if label not in providers:
                        error[label] = construction_errors.get(label, "not configured")
                        latency_first[label] = latency_complete[label] = None
                        reply[label] = None
                        reply_chars[label] = 0
                        spoken_sentences[label] = 0
                        markup_leaked[label] = False
                        continue
                    try:
                        response, latency_ms = await _call_provider(
                            providers[label],
                            system_prompt=system_prompt,
                            history=question["history"],
                            text=question["text"],
                            context=context,
                        )
                        latency_first[label] = latency_ms
                        latency_complete[label] = latency_ms
                        reply[label] = response.text
                        reply_chars[label] = len(response.text)
                        spoken_sentences[label] = len(speech_chunks(response.text))
                        markup_leaked[label] = _markup_leaked(response.text)
                        error[label] = None
                    except LLMProviderError as exc:
                        error[label] = str(exc)
                        latency_first[label] = latency_complete[label] = None
                        reply[label] = None
                        reply_chars[label] = 0
                        spoken_sentences[label] = 0
                        markup_leaked[label] = False

                results.append(
                    QuestionResult(
                        question_id=question["id"],
                        text=question["text"],
                        retrieval_count=len(retrieved),
                        document_ids=sorted({str(r.document_id) for r in retrieved}),
                        chunk_ids=[str(r.chunk_id) for r in retrieved],
                        latency_first_ms=latency_first,
                        latency_complete_ms=latency_complete,
                        reply=reply,
                        reply_chars=reply_chars,
                        spoken_sentences=spoken_sentences,
                        markup_leaked=markup_leaked,
                        error=error,
                    )
                )
        finally:
            if seeded_document is not None:
                await db.delete(seeded_document)
                await db.commit()

    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "provider_a": {"requested": args.provider_a, "error": construction_errors.get("A")},
        "provider_b": {"requested": args.provider_b, "error": construction_errors.get("B")},
        "seeded_demo_kb": args.seed_demo_kb,
        "questions": [asdict(r) for r in results],
    }


def _avg(values: list[int]) -> float | None:
    return sum(values) / len(values) if values else None


def _markdown_table(report: dict) -> str:
    questions = report["questions"]

    def _metric(label: str, extractor) -> str:
        values = [extractor(q) for q in questions]
        numeric = [v for v in values if isinstance(v, (int, float))]
        return f"{_avg(numeric):.0f}" if numeric else "n/a (provider unavailable)"

    a_ok = sum(1 for q in questions if q["error"]["A"] is None)
    b_ok = sum(1 for q in questions if q["error"]["B"] is None)
    a_grounded = sum(1 for q in questions if q["error"]["A"] is None and q["retrieval_count"] > 0)
    b_grounded = sum(1 for q in questions if q["error"]["B"] is None and q["retrieval_count"] > 0)
    a_markup = sum(1 for q in questions if q["markup_leaked"]["A"])
    b_markup = sum(1 for q in questions if q["markup_leaked"]["B"])

    lines = [
        f"| Metric | {report['provider_a']['requested']} (A) | {report['provider_b']['requested']} (B) |",
        "|---|---:|---:|",
        f"| First response latency (ms, avg) | {_metric('A', lambda q: q['latency_first_ms']['A'])} | {_metric('B', lambda q: q['latency_first_ms']['B'])} |",
        f"| Full response latency (ms, avg) | {_metric('A', lambda q: q['latency_complete_ms']['A'])} | {_metric('B', lambda q: q['latency_complete_ms']['B'])} |",
        f"| Avg characters | {_metric('A', lambda q: q['reply_chars']['A'])} | {_metric('B', lambda q: q['reply_chars']['B'])} |",
        f"| Avg spoken sentences | {_metric('A', lambda q: q['spoken_sentences']['A'])} | {_metric('B', lambda q: q['spoken_sentences']['B'])} |",
        f"| Answered successfully | {a_ok}/{len(questions)} | {b_ok}/{len(questions)} |",
        f"| RAG-grounded answers (retrieval_count>0) | {a_grounded}/{len(questions)} | {b_grounded}/{len(questions)} |",
        f"| Markup/punctuation leakage | {a_markup}/{len(questions)} | {b_markup}/{len(questions)} |",
        "| Hindi naturalness | manual review required | manual review required |",
        "| Follow-up handling | manual review required | manual review required |",
        "| Interruption compatibility | see tests/test_barge_in_with_llm_providers.py (provider-agnostic by construction) |||",
    ]
    return "\n".join(lines)


async def main_async(args) -> int:
    report = await run(args)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "llm_ab_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    table = _markdown_table(report)
    (args.out / "llm_ab_table.md").write_text(table, encoding="utf-8")
    print(table)
    print()
    print(f"Full report: {args.out / 'llm_ab_report.json'}")
    return 0


async def _run_and_dispose(args) -> int:
    try:
        return await main_async(args)
    finally:
        await engine.dispose()


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("/tmp/sunway-llm-ab"))
    parser.add_argument("--provider-a", default="gemini")
    parser.add_argument("--provider-b", default="sarvam_m")
    parser.add_argument(
        "--seed-demo-kb",
        action="store_true",
        help="temporarily ingest a small built-in LSD reference so RAG grounding has something "
        "to measure on a machine with no knowledge base loaded; removed again when the run ends",
    )
    args = parser.parse_args()
    return asyncio.run(_run_and_dispose(args))


if __name__ == "__main__":
    raise SystemExit(main())
