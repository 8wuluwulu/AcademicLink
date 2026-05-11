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
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.db.database import init_db, async_session_factory
from app.services.tutor_service import ensure_tutor_exists

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
    if settings.is_testing:
        logger.info("Testing mode active — skipping real database init.")
    else:
        logger.info("Initialising database…")
        try:
            await init_db()
            logger.info("Database ready.")

            # Seed default tutor if the table is empty
            async with async_session_factory() as session:
                await ensure_tutor_exists(session)
        except Exception as exc:
            logger.error("Database initialisation failed: %s", exc)

    bot_task: asyncio.Task | None = None

    if settings.is_testing:
        logger.info("Testing mode active — skipping Telegram bot.")
    elif not settings.bot_token:
        logger.warning(
            "BOT_TOKEN is not set — Telegram bot will NOT start. "
            "Set BOT_TOKEN in your .env file to enable the bot."
        )
    else:
        # Import Aiogram only when we actually have a token
        from aiogram import Bot, Dispatcher
        from app.bot.handlers import router as bot_router
        from app.core.bot import set_bot

        bot = Bot(token=settings.bot_token)
        set_bot(bot)  # Make bot accessible via get_bot() helper
        application.state.bot = bot  # Also store on app.state for request access
        dp = Dispatcher()
        try:
            dp.include_router(bot_router)
        except RuntimeError:
            # Handle "Router is already attached" during tests/reloads
            pass

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

    # --- Scheduler ---
    if settings.is_testing:
        logger.info("Testing mode active — skipping scheduler.")
    else:
        from app.core.scheduler import scheduler, configure_scheduler

        configure_scheduler()
        scheduler.start()
        logger.info("APScheduler started with %d jobs.", len(scheduler.get_jobs()))

    yield  # ← application is running

    # --- Scheduler Shutdown ---
    if not settings.is_testing:
        from app.core.scheduler import scheduler
        if scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("APScheduler shut down.")

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

# --- CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
