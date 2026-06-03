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

    # Check students columns
    if 'students' in inspector.get_table_names():
        students_cols = [c['name'] for c in inspector.get_columns('students')]
        if 'tutor_id' not in students_cols:
            # 1. Add tutor_id column (initially nullable to allow creation)
            connection.execute(text("ALTER TABLE students ADD COLUMN tutor_id INTEGER"))
            
            # 2. Seed tutor_id for existing students (associate with the first tutor)
            connection.execute(text(
                "UPDATE students SET tutor_id = (SELECT id FROM tutors LIMIT 1) WHERE tutor_id IS NULL"
            ))
            
            # Make tutor_id NOT NULL and add foreign key constraint
            connection.execute(text(
                "ALTER TABLE students ALTER COLUMN tutor_id SET NOT NULL"
            ))
            connection.execute(text(
                "ALTER TABLE students ADD CONSTRAINT fk_students_tutor_id FOREIGN KEY (tutor_id) REFERENCES tutors(id) ON DELETE CASCADE"
            ))
            
            # 3. Drop existing global unique constraints and unique indexes
            connection.execute(text("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_phone_key"))
            connection.execute(text("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_telegram_id_key"))
            connection.execute(text("DROP INDEX IF EXISTS ix_students_phone"))
            connection.execute(text("DROP INDEX IF EXISTS ix_students_telegram_id"))
            
            # Create non-unique indexes for fast lookups
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_students_phone ON students (phone)"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_students_telegram_id ON students (telegram_id)"))
            
            # 4. Add composite uniqueness constraints
            connection.execute(text(
                "ALTER TABLE students ADD CONSTRAINT uq_students_tutor_phone UNIQUE (tutor_id, phone)"
            ))
            connection.execute(text(
                "ALTER TABLE students ADD CONSTRAINT uq_students_tutor_telegram_id UNIQUE (tutor_id, telegram_id)"
            ))

        # Always check and repair unique indexes to be non-unique if they exist
        indexes = inspector.get_indexes('students')
        for index in indexes:
            if index['name'] in ['ix_students_phone', 'ix_students_telegram_id'] and index['unique']:
                connection.execute(text(f"DROP INDEX IF EXISTS {index['name']}"))
                col_name = 'phone' if index['name'] == 'ix_students_phone' else 'telegram_id'
                connection.execute(text(f"CREATE INDEX IF NOT EXISTS {index['name']} ON students ({col_name})"))


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
