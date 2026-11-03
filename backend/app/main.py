"""FastAPI application entry point."""

import logging
import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.bot_routes import router as bot_router
from app.api.routes import router
from app.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("meetmind")


def _origin_regex(extension_ids: list[str]) -> str:
    if not extension_ids:
        return r"^chrome-extension://[a-p]{32}$"
    ids = "|".join(re.escape(i.strip()) for i in extension_ids if i.strip())
    return rf"^chrome-extension://({ids})$"


def create_app() -> FastAPI:
    settings = get_settings()
    for problem in settings.problems():
        logger.warning("Config problem: %s", problem)

    app = FastAPI(title=settings.app_name, version=settings.app_version, docs_url="/docs", redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=_origin_regex(settings.cors.allowed_extension_ids),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-MeetMind-Token", "X-MeetMind-Client"],
        expose_headers=["Content-Disposition"],
    )
    app.include_router(router)
    app.include_router(bot_router)
    return app
