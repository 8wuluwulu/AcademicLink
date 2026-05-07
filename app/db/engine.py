"""
AcademicLink — Database Engine & Session Factory

Provides an async SQLAlchemy engine and a session dependency
for both FastAPI (Depends) and standalone bot usage.
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


async def init_db() -> None:
    """Create all tables defined via SQLModel metadata."""
    # Import models so SQLModel registers them before create_all
    import app.db.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an async DB session."""
    async with async_session_factory() as session:
        yield session
