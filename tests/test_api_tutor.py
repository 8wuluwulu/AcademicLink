"""
AcademicLink — Direct Unit Tests for Tutor API Endpoints
"""

from datetime import date as dt_date, datetime, time, timezone, timedelta
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.db.models import Tutor, Service, Student, StudentTutorLink, AvailabilitySlot
from app.api.tutor import (
    list_tutors,
    get_tutors_by_student,
    get_tutor,
    list_tutor_services,
    get_tutor_slots,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def test_engine():
    """In-memory SQLite for integration tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def test_session_factory(test_engine):
    """Session factory bound to the test engine."""
    return async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )


@pytest_asyncio.fixture
async def db_session(test_session_factory):
    """Seed tutor + service + student + link and return session."""
    async with test_session_factory() as session:
        t1 = Tutor(
            id=1,
            tg_id=111,
            name="Active Tutor",
            is_active=True,
            subscription_status="active",
            subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        t2 = Tutor(
            id=2,
            tg_id=222,
            name="Inactive Tutor",
            is_active=False,
            subscription_status="active",
            subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        session.add_all([t1, t2])
        await session.flush()

        s1 = Service(
            id=1,
            tutor_id=1,
            name="Math Service",
            duration=60,
            buffer_time=0,
            price=1500,
            is_active=True,
        )
        s2 = Service(
            id=2,
            tutor_id=1,
            name="Inactive Service",
            duration=45,
            buffer_time=0,
            price=1000,
            is_active=False,
        )
        session.add_all([s1, s2])
        await session.flush()

        student = Student(
            id=1,
            full_name="Tutor Student",
            phone="+79001112233",
            telegram_id=333,
        )
        session.add(student)
        await session.flush()

        link = StudentTutorLink(
            student_id=1,
            tutor_id=1,
            is_active=True,
        )
        session.add(link)

        # Seed availability slot for Monday
        slot = AvailabilitySlot(
            tutor_id=1,
            weekday=0,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        session.add(slot)

        await session.commit()

    async with test_session_factory() as session:
        yield session


# ── Tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_tutors(db_session):
    tutors = await list_tutors(session=db_session)
    assert len(tutors) == 1
    assert tutors[0].name == "Active Tutor"


@pytest.mark.asyncio
async def test_get_tutors_by_student_empty(db_session):
    res = await get_tutors_by_student(telegram_id=None, phone=None, session=db_session)
    assert res == []


@pytest.mark.asyncio
async def test_get_tutors_by_student_by_telegram_id(db_session):
    tutors = await get_tutors_by_student(telegram_id=333, phone=None, session=db_session)
    assert len(tutors) == 1
    assert tutors[0].name == "Active Tutor"


@pytest.mark.asyncio
async def test_get_tutors_by_student_by_phone(db_session):
    # Test normal search
    tutors = await get_tutors_by_student(telegram_id=None, phone="+79001112233", session=db_session)
    assert len(tutors) == 1
    assert tutors[0].name == "Active Tutor"

    # Test phone normalization: 8xxx -> 7xxx (lines 89-90)
    tutors2 = await get_tutors_by_student(telegram_id=None, phone="89001112233", session=db_session)
    assert len(tutors2) == 1

    # Test phone normalization: 10 digits (lines 91-92)
    tutors3 = await get_tutors_by_student(telegram_id=None, phone="9001112233", session=db_session)
    assert len(tutors3) == 1


@pytest.mark.asyncio
async def test_get_tutor_success(db_session):
    tutor = await get_tutor(tutor_id=1, session=db_session)
    assert tutor.name == "Active Tutor"


@pytest.mark.asyncio
async def test_get_tutor_not_found(db_session):
    with pytest.raises(HTTPException) as exc_info:
        await get_tutor(tutor_id=999, session=db_session)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_list_tutor_services(db_session):
    services = await list_tutor_services(tutor_id=1, session=db_session)
    assert len(services) == 1
    assert services[0].name == "Math Service"


@pytest.mark.asyncio
async def test_get_tutor_slots(db_session):
    # Monday 22 June 2026
    date_monday = dt_date(2026, 6, 22)
    res = await get_tutor_slots(tutor_id=1, service_id=1, date=date_monday, session=db_session)
    assert res.tutor_id == 1
    assert res.date == date_monday
    # Should find available slots on Monday
    assert len(res.available_slots) > 0
