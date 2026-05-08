"""
AcademicLink — API Router Aggregator

Collects all sub-routers and exposes a single `router` instance
to be included in the FastAPI application.
"""

from fastapi import APIRouter

from app.api.booking import router as booking_router

router = APIRouter(prefix="/api/v1")

# ── Sub-routers ──────────────────────────────────────────────────────
router.include_router(booking_router)


@router.get("/health", tags=["system"])
async def health_check():
    """Lightweight liveness probe."""
    return {"status": "ok"}

