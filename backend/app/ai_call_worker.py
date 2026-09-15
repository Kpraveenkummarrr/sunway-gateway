"""Standalone entrypoint for the AI call controller.

Runs independently of the FastAPI web server — it holds a long-lived ARI
WebSocket connection and reacts to Asterisk call events, which doesn't
fit an HTTP request/response cycle. Run it alongside `uvicorn` on the
same host as Asterisk:

    python -m app.ai_call_worker

Requires ASTERISK_ARI_USERNAME/ASTERISK_ARI_PASSWORD to be set (see
docs/asterisk.md for how ARI is configured) and STT_PROVIDER/LLM_PROVIDER/
TTS_PROVIDER/RAG_EMBEDDING_PROVIDER to be configured (mock or a real
vendor) — this worker fails fast at startup if any of them aren't, same
as the HTTP API does per-request.
"""

import asyncio

from app.core.config import get_settings
from app.core.db import AsyncSessionLocal
from app.core.logging import get_logger
from app.providers.embeddings.factory import get_embedding_provider
from app.providers.llm.factory import get_llm_provider
from app.providers.stt.factory import get_stt_provider
from app.providers.tts.factory import get_tts_provider
from app.services.ari_client import AriClient
from app.services.call_controller import AICallController

logger = get_logger(__name__)


async def main() -> None:
    settings = get_settings()

    if not settings.asterisk_ari_username or not settings.asterisk_ari_password:
        raise SystemExit(
            "ASTERISK_ARI_USERNAME/ASTERISK_ARI_PASSWORD are not set — "
            "see docs/asterisk.md for ARI setup."
        )

    # Fail fast on misconfigured providers rather than accepting calls we
    # can't actually process.
    embedding_provider = get_embedding_provider(settings)
    llm_provider = get_llm_provider(settings)
    stt_provider = get_stt_provider(settings)
    tts_provider = get_tts_provider(settings)

    ari = AriClient(
        base_url=settings.resolved_ari_url(),
        username=settings.asterisk_ari_username,
        password=settings.asterisk_ari_password,
        app=settings.asterisk_ari_app,
    )
    controller = AICallController(
        ari=ari,
        settings=settings,
        session_factory=AsyncSessionLocal,
        embedding_provider=embedding_provider,
        llm_provider=llm_provider,
        stt_provider=stt_provider,
        tts_provider=tts_provider,
    )

    logger.info(
        "AI call worker starting: ari=%s app=%s test_extension=%s language=%s "
        "stt=%s llm=%s tts=%s embeddings=%s",
        settings.resolved_ari_url(),
        settings.asterisk_ari_app,
        settings.ai_test_extension,
        settings.ai_language,
        settings.stt_provider,
        settings.llm_provider,
        settings.tts_provider,
        settings.rag_embedding_provider,
    )
    if settings.ai_language == "hi":
        for kind in ("welcome", "error", "goodbye"):
            message = settings.caller_message(kind)
            if not any("ऀ" <= ch <= "ॿ" for ch in message):
                logger.warning(
                    "AI_LANGUAGE=hi but the %s message has no Devanagari text — callers will hear it "
                    "as-is. Remove AI_%s_MESSAGE from .env to use the built-in Hindi phrase.",
                    kind,
                    kind.upper(),
                )
    try:
        await controller.run_forever()
    finally:
        await ari.aclose()


if __name__ == "__main__":
    asyncio.run(main())
