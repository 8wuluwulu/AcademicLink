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
def _auto_migrate_columns(connection) -> None:
    """Helper to inspect existing tables and add missing columns dynamically."""
    from sqlalchemy import inspect, text
    inspector = inspect(connection)
    
    # Check tutors columns
    if 'tutors' in inspector.get_table_names():
        tutors_cols = [c['name'] for c in inspector.get_columns('tutors')]
        if 'google_token_json' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN google_token_json TEXT"))
        if 'google_calendar_id' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN google_calendar_id VARCHAR(255) DEFAULT 'primary'"))
        if 'subscription_expires_at' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN subscription_expires_at TIMESTAMP WITH TIME ZONE"))
        if 'subscription_status' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN subscription_status VARCHAR(50) DEFAULT 'trial'"))
        if 'bio' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN bio TEXT"))
        if 'subject' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN subject VARCHAR(255)"))
        if 'avatar_url' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN avatar_url VARCHAR(512)"))
        if 'accent_color' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN accent_color VARCHAR(10) DEFAULT '#4f46e5'"))
        if 'sbp_phone' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN sbp_phone VARCHAR(20)"))
        if 'sbp_bank' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN sbp_bank VARCHAR(100)"))
        if 'sbp_qr_url' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN sbp_qr_url VARCHAR(512)"))
        if 'sbp_link' not in tutors_cols:
            connection.execute(text("ALTER TABLE tutors ADD COLUMN sbp_link VARCHAR(512)"))
            
    # Check bookings columns
    if 'bookings' in inspector.get_table_names():
        bookings_cols = [c['name'] for c in inspector.get_columns('bookings')]
        if 'payment_method' not in bookings_cols:
            connection.execute(text("ALTER TABLE bookings ADD COLUMN payment_method VARCHAR(20) DEFAULT 'cash'"))
        if 'google_event_id' not in bookings_cols:
            connection.execute(text("ALTER TABLE bookings ADD COLUMN google_event_id VARCHAR(255)"))
        if 'payment_comment' not in bookings_cols:
            connection.execute(text("ALTER TABLE bookings ADD COLUMN payment_comment TEXT"))


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
        await conn.run_sync(_auto_migrate_columns)


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
