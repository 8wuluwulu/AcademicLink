"""
AcademicLink — Database Utility

Asynchronous engine, session factory, and table initialisation.
This is the canonical database module; prefer importing from here.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from app.core.config import settings

# ── Engine ───────────────────────────────────────────────────────────
engine = create_async_engine(
    settings.database_url,
    echo=not settings.is_production,
    future=True,
)

# ── Session Factory ──────────────────────────────────────────────────
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Table Creation ───────────────────────────────────────────────────
async def init_db() -> None:
    """
    Create all tables registered in SQLModel metadata.

    Models are imported inside the function so they are registered
    with SQLModel.metadata *before* ``create_all`` runs.  The sync
    ``create_all`` call is executed via ``run_sync`` to stay
    compatible with the async engine.
    """
    import app.db.models  # noqa: F401  — registers table classes

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


# ── Dependency ───────────────────────────────────────────────────────
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency — yields an ``AsyncSession``.

    Usage::

        @router.get("/items")
        async def list_items(session: AsyncSession = Depends(get_session)):
            ...
    """
    async with async_session_factory() as session:
        yield session
