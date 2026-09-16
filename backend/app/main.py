from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.admin import router as admin_router
from app.api.callcentre import router as callcentre_router
from app.api.conversation import router as conversation_router
from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    logger.info("startup app_env=%s", settings.app_env)
    yield


app = FastAPI(title="Sunway GSM Gateway — AI Voice Agent Backend", lifespan=lifespan)

app.include_router(health_router)
app.include_router(knowledge_router)
app.include_router(conversation_router)
app.include_router(callcentre_router)
app.include_router(admin_router)
