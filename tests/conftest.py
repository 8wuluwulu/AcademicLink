"""
AcademicLink — Test Configuration

Shared fixtures for the test suite:
- In-memory async SQLite engine & session
- Seeded tutor, student, and availability slot data
- Mocked bot instance
"""

import asyncio
import os
from datetime import time, datetime, timezone, timedelta

# Set environment to testing BEFORE any other imports that might load settings
os.environ["ENVIRONMENT"] = "testing"

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from app.db.models import AvailabilitySlot, Student, Tutor, StudentTutorLink


# ── Event loop for async tests ───────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    """Use a single event loop for the entire test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Async SQLite engine (in-memory) ─────────────────────────────────

@pytest_asyncio.fixture
async def async_engine():
    """Create an in-memory async SQLite engine for testing."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(async_engine):
    """Provide a fresh async session for each test."""
    async_session = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with async_session() as sess:
        yield sess


# ── Seed Data ────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def seeded_session(session: AsyncSession):
    """
    Seed the database with a tutor, student, and availability slots.

    Availability:
    - Monday (0): 09:00–17:00
    - Wednesday (2): 10:00–18:00
    - Friday (4): 08:00–16:00
    """
    tutor = Tutor(
        tg_id=123456789,
        name="Test Tutor",
        is_active=True,
        subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        subscription_status="active",
    )
    session.add(tutor)
    await session.flush()

    from app.db.models import Service
    default_service = Service(
        id=1,
        tutor_id=tutor.id,
        name="Test Service",
        duration=60,
        buffer_time=0,
        price=1000,
        is_active=True
    )
    session.add(default_service)
    await session.flush()

    student = Student(
        full_name="Test Student",
        phone="+79001234567",
        telegram_id=987654321,
    )
    session.add(student)
    await session.flush()

    link = StudentTutorLink(
        student_id=student.id,
        tutor_id=tutor.id,
        is_active=True,
    )
    session.add(link)
    await session.flush()

    # Add availability slots
    for weekday, start, end in [
        (0, time(9, 0), time(17, 0)),   # Monday
        (2, time(10, 0), time(18, 0)),  # Wednesday
        (4, time(8, 0), time(16, 0)),   # Friday
    ]:
        slot = AvailabilitySlot(
            tutor_id=tutor.id,
            weekday=weekday,
            start_time=start,
            end_time=end,
        )
        session.add(slot)

    await session.commit()
    yield session
