"""
AcademicLink — Direct Unit Tests for Booking API Endpoints
"""

import logging
from datetime import datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.config import settings
from app.db.models import AvailabilitySlot, Booking, BookingStatus, Service, Student, StudentTutorLink, Tutor
from app.api.booking import (
    BookingCreate,
    verify_api_key,
    notify_tutor_new_booking,
    create_booking,
    get_reschedule_info,
    reschedule_from_web,
)

logger = logging.getLogger(__name__)


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
    """Seed tutor + student + availability + booking and return session."""
    async with test_session_factory() as session:
        tutor = Tutor(
            id=1,
            tg_id=111222333,
            name="API Tutor",
            is_active=True,
            subscription_status="active",
            subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        )
        session.add(tutor)
        await session.flush()

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
            id=1,
            full_name="Existing Student",
            phone="+79001234567",
            telegram_id=987654321,
            telegram_username="student_username",
        )
        session.add(student)
        await session.flush()

        link = StudentTutorLink(
            student_id=student.id,
            tutor_id=tutor.id,
            is_active=True,
        )
        session.add(link)

        slot = AvailabilitySlot(
            tutor_id=tutor.id,
            weekday=0,  # Monday
            start_time=time(8, 0),
            end_time=time(20, 0),
        )
        session.add(slot)

        booking = Booking(
            id=10,
            student_id=student.id,
            tutor_id=tutor.id,
            service_id=default_service.id,
            service_type="Test Service",
            appointment_time=datetime(2026, 6, 22, 15, 0, tzinfo=timezone.utc),  # Monday 15:00 UTC
            status=BookingStatus.PENDING,
            payment_method="cash",
            student_name_snapshot="Existing Student"
        )
        session.add(booking)
        await session.commit()

    async with test_session_factory() as session:
        yield session


# ── Validation & Security Tests ──────────────────────────────────────

def test_booking_create_validation():
    # Test valid phone
    p = BookingCreate(
        full_name="Ivan",
        phone="+79001234567",
        service_id=1,
        appointment_time=datetime.now(),
        tutor_id=1,
        payment_method="cash",
    )
    assert p.phone == "+79001234567"

    # Test normalization cases
    for raw_phone in ["89001234567", "9001234567", "+89001234567"]:
        p_norm = BookingCreate(
            full_name="Ivan",
            phone=raw_phone,
            service_id=1,
            appointment_time=datetime.now(),
            tutor_id=1,
            payment_method="cash",
        )
        assert p_norm.phone == "+79001234567"

    # Test phone is None (line 93)
    p_none = BookingCreate(
        full_name="Ivan",
        phone=None,
        service_id=1,
        appointment_time=datetime.now(),
        tutor_id=1,
        payment_method="cash",
    )
    assert p_none.phone is None

    # Test invalid phone format (Russian landline)
    with pytest.raises(ValueError):
        BookingCreate(
            full_name="Ivan",
            phone="+74951234567",
            service_id=1,
            appointment_time=datetime.now(),
            tutor_id=1,
            payment_method="cash",
        )

    # Test invalid phone format (foreign number)
    with pytest.raises(ValueError):
        BookingCreate(
            full_name="Ivan",
            phone="+49109218362",
            service_id=1,
            appointment_time=datetime.now(),
            tutor_id=1,
            payment_method="cash",
        )

    # Test invalid phone format
    with pytest.raises(ValueError):
        BookingCreate(
            full_name="Ivan",
            phone="1234567",
            service_id=1,
            appointment_time=datetime.now(),
            tutor_id=1,
            payment_method="cash",
        )

    # Test invalid payment method (lines 104-107)
    with pytest.raises(ValueError):
        BookingCreate(
            full_name="Ivan",
            phone="+79001234567",
            service_id=1,
            appointment_time=datetime.now(),
            tutor_id=1,
            payment_method="invalid_method",
        )


@pytest.mark.asyncio
async def test_verify_api_key():
    with patch.object(settings, "secret_key", "secret123"):
        res = await verify_api_key("secret123")
        assert res == "secret123"

        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key("badsecret")
        assert exc_info.value.status_code == 403


# ── Notification Tests ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notify_tutor_new_booking_tutor_not_found(db_session):
    booking = Booking(tutor_id=9999)
    bot = MagicMock()
    # Should exit early (line 138)
    await notify_tutor_new_booking(booking, db_session, bot)
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_notify_tutor_new_booking_p2p_success(db_session):
    booking_stmt = (
        select(Booking)
        .where(Booking.id == 10)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    res = await db_session.execute(booking_stmt)
    booking = res.scalar_one()

    booking.payment_method = "transfer"
    booking.payment_comment = "Sender Name"
    
    bot = AsyncMock()
    await notify_tutor_new_booking(booking, db_session, bot)
    bot.send_message.assert_called_once()
    assert "СБП" in bot.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_notify_tutor_new_booking_p2p_exception(db_session):
    booking_stmt = (
        select(Booking)
        .where(Booking.id == 10)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    res = await db_session.execute(booking_stmt)
    booking = res.scalar_one()

    booking.payment_method = "online"
    
    bot = AsyncMock()
    bot.send_message.side_effect = Exception("Bot error")
    # Exception should be caught and logged (line 173)
    await notify_tutor_new_booking(booking, db_session, bot)


@pytest.mark.asyncio
async def test_notify_tutor_new_booking_cash_exception(db_session):
    booking_stmt = (
        select(Booking)
        .where(Booking.id == 10)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    res = await db_session.execute(booking_stmt)
    booking = res.scalar_one()

    booking.payment_method = "cash"
    
    bot = AsyncMock()
    bot.send_message.side_effect = Exception("Bot error")
    # Exception should be caught and logged (line 187)
    await notify_tutor_new_booking(booking, db_session, bot)


# ── Create Booking Endpoint Tests ────────────────────────────────────

@pytest.mark.asyncio
async def test_create_booking_value_error(db_session):
    payload = BookingCreate(
        full_name="Student",
        phone="+79001234567",
        service_id=1,
        appointment_time=datetime.now(),
        tutor_id=1,
        payment_method="cash",
    )
    request = MagicMock(spec=Request)
    
    with patch("app.api.booking.create_booking_from_web", side_effect=ValueError("Tutor not active")):
        with pytest.raises(HTTPException) as exc_info:
            await create_booking(payload, request, db_session)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Tutor not active"


@pytest.mark.asyncio
async def test_create_booking_success(db_session):
    payload = BookingCreate(
        full_name="New Student",
        phone="+79001234567",
        service_id=1,
        appointment_time=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        tutor_id=1,
        payment_method="cash",
    )
    
    # Mock bot
    bot = AsyncMock()
    bot.get_me.return_value = MagicMock(username="test_bot")
    request = MagicMock(spec=Request)
    request.app.state.bot = bot

    with patch("app.services.google_calendar_service.sync_booking_to_calendar", side_effect=Exception("Sync failed")):
        response = await create_booking(payload, request, db_session)
        assert response.id is not None


@pytest.mark.asyncio
async def test_create_booking_student_notification_fails(db_session):
    # Retrieve student and set telegram_id to trigger student notification path
    student = await db_session.get(Student, 1)
    student.telegram_id = 987654321
    await db_session.commit()

    payload = BookingCreate(
        full_name="Existing Student",
        phone="+79001234567",
        service_id=1,
        appointment_time=datetime(2026, 6, 22, 11, 0, tzinfo=timezone.utc),
        tutor_id=1,
        payment_method="cash",
    )
    
    bot = AsyncMock()
    bot.send_message.side_effect = [
        None,  # tutor notification succeeds
        Exception("Student notification fails")  # student notification fails
    ]
    request = MagicMock(spec=Request)
    request.app.state.bot = bot

    with patch("app.services.google_calendar_service.sync_booking_to_calendar", AsyncMock()):
        response = await create_booking(payload, request, db_session)
        assert response.id is not None


# ── Reschedule Info Endpoint Tests ───────────────────────────────────

@pytest.mark.asyncio
async def test_get_reschedule_info_not_found(db_session):
    with pytest.raises(HTTPException) as exc_info:
        await get_reschedule_info(booking_id=9999, session=db_session)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_reschedule_info_success(db_session):
    info = await get_reschedule_info(booking_id=10, session=db_session)
    assert info.tutor_id == 1
    assert info.student_name == "Existing Student"


# ── Reschedule from Web Endpoint Tests ───────────────────────────────

from app.api.booking import RescheduleWebRequest

@pytest.mark.asyncio
async def test_reschedule_from_web_not_found(db_session):
    payload = RescheduleWebRequest(appointment_time=datetime.now())
    request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc_info:
        await reschedule_from_web(booking_id=9999, payload=payload, request=request, session=db_session)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_reschedule_from_web_tutor_mode_success(db_session):
    new_time = datetime(2026, 6, 22, 16, 0)
    payload = RescheduleWebRequest(appointment_time=new_time, tutor_mode=True)
    
    bot = AsyncMock()
    request = MagicMock(spec=Request)
    request.app.state.bot = bot

    with patch("app.services.google_calendar_service.sync_booking_to_calendar", AsyncMock()) as mock_sync:
        res = await reschedule_from_web(booking_id=10, payload=payload, request=request, session=db_session)
        assert res == {"status": "success"}
        mock_sync.assert_called_once()
        assert bot.send_message.call_count == 2


@pytest.mark.asyncio
async def test_reschedule_from_web_tutor_mode_errors(db_session):
    new_time = datetime(2026, 6, 22, 16, 0, tzinfo=timezone.utc)
    payload = RescheduleWebRequest(appointment_time=new_time, tutor_mode=True)
    
    request = MagicMock(spec=Request)
    with patch("app.services.booking_service.reschedule_booking", side_effect=ValueError("Reschedule failed")):
        with pytest.raises(HTTPException) as exc_info:
            await reschedule_from_web(booking_id=10, payload=payload, request=request, session=db_session)
        assert exc_info.value.status_code == 400

    bot = AsyncMock()
    bot.send_message.side_effect = Exception("Bot error")
    request.app.state.bot = bot

    with patch("app.services.google_calendar_service.sync_booking_to_calendar", side_effect=Exception("Sync error")):
        res = await reschedule_from_web(booking_id=10, payload=payload, request=request, session=db_session)
        assert res == {"status": "success"}


@pytest.mark.asyncio
async def test_reschedule_from_web_student_mode_access_denied(db_session):
    link_stmt = select(StudentTutorLink).where(StudentTutorLink.student_id == 1, StudentTutorLink.tutor_id == 1)
    res = await db_session.execute(link_stmt)
    obj = res.scalar_one()
    obj.is_active = False
    await db_session.commit()

    payload = RescheduleWebRequest(appointment_time=datetime.now(), is_student=True)
    request = MagicMock(spec=Request)
    
    with pytest.raises(HTTPException) as exc_info:
        await reschedule_from_web(booking_id=10, payload=payload, request=request, session=db_session)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_reschedule_from_web_student_mode_validation_error(db_session):
    payload = RescheduleWebRequest(appointment_time=datetime.now(), is_student=True)
    request = MagicMock(spec=Request)
    
    with patch("app.services.booking_service.check_availability", side_effect=ValueError("Tutor not available")):
        with pytest.raises(HTTPException) as exc_info:
            await reschedule_from_web(booking_id=10, payload=payload, request=request, session=db_session)
        assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_reschedule_from_web_student_mode_success(db_session):
    new_time = datetime(2026, 6, 22, 17, 0, tzinfo=timezone.utc)
    payload = RescheduleWebRequest(appointment_time=new_time, is_student=True)
    
    bot = AsyncMock()
    request = MagicMock(spec=Request)
    request.app.state.bot = bot

    with patch("app.services.booking_service.check_availability", AsyncMock()), \
         patch("app.services.booking_service.check_tutor_absence", AsyncMock()), \
         patch("app.services.booking_service.check_double_booking", AsyncMock()):
        
        res = await reschedule_from_web(booking_id=10, payload=payload, request=request, session=db_session)
        assert res == {"status": "success"}
        assert bot.send_message.call_count == 2


@pytest.mark.asyncio
async def test_reschedule_from_web_student_mode_bot_errors(db_session):
    new_time = datetime(2026, 6, 22, 17, 0, tzinfo=timezone.utc)
    payload = RescheduleWebRequest(appointment_time=new_time, is_student=True)
    
    bot = AsyncMock()
    bot.send_message.side_effect = Exception("Bot error")
    request = MagicMock(spec=Request)
    request.app.state.bot = bot

    with patch("app.services.booking_service.check_availability", AsyncMock()), \
         patch("app.services.booking_service.check_tutor_absence", AsyncMock()), \
         patch("app.services.booking_service.check_double_booking", AsyncMock()):
        
        res = await reschedule_from_web(booking_id=10, payload=payload, request=request, session=db_session)
        assert res == {"status": "success"}
