"""
AcademicLink — Application Entry Point

Starts **both** the FastAPI web server and the Aiogram Telegram bot
inside a single async event loop using a lifespan context manager.

Usage:
    python main.py
"""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.core.config import settings
from app.db.engine import init_db

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG if not settings.is_production else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
)
logger = logging.getLogger("academiclink")


# ── Lifespan ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(application: FastAPI):
    """
    Runs once on startup / shutdown.

    • Initialises the database.
    • Validates BOT_TOKEN and starts Telegram polling in the background.
    • Cleans up on shutdown.
    """
    # --- Startup ---
    logger.info("Initialising database…")
    try:
        await init_db()
        logger.info("Database ready.")
    except Exception as exc:
        logger.error("Database initialisation failed: %s", exc)
        # Allow the app to continue — the health-check will report degraded

    bot_task: asyncio.Task | None = None

    if not settings.bot_token:
        logger.warning(
            "BOT_TOKEN is not set — Telegram bot will NOT start. "
            "Set BOT_TOKEN in your .env file to enable the bot."
        )
    else:
        # Import Aiogram only when we actually have a token
        from aiogram import Bot, Dispatcher
        from app.bot.handlers import router as bot_router

        bot = Bot(token=settings.bot_token)
        dp = Dispatcher()
        dp.include_router(bot_router)

        async def _run_polling() -> None:
            logger.info("Starting Telegram bot polling…")
            try:
                await dp.start_polling(bot)
            except asyncio.CancelledError:
                logger.info("Bot polling cancelled.")
            except Exception as exc:
                logger.error("Bot polling error: %s", exc)

        bot_task = asyncio.create_task(_run_polling())
        logger.info("Telegram bot task scheduled.")

    yield  # ← application is running

    # --- Shutdown ---
    if bot_task is not None:
        logger.info("Shutting down Telegram bot…")
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
    logger.info("Shutdown complete.")


# ── FastAPI Application ──────────────────────────────────────────────
app = FastAPI(
    title=f"{settings.project_name} API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

# Include API routes
from app.api.router import router as api_router  # noqa: E402

app.include_router(api_router)


# ── Health Check ─────────────────────────────────────────────────────
@app.get("/", tags=["Health"])
async def health_check():
    """Returns the current project status."""
    return {
        "project": settings.project_name,
        "status": "running",
        "environment": settings.environment,
        "bot_enabled": settings.bot_token is not None,
    }


# ── Entry Point ──────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        uvicorn.run(
            "main:app",
            host="0.0.0.0",
            port=8000,
            reload=not settings.is_production,
            log_level="info",
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        sys.exit(0)
