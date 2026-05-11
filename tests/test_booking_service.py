"""
AcademicLink — Unit Tests for Booking Service

Covers:
- Successful booking creation
- Availability slot rejection
- Double-booking / overlap rejection
"""

from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.models import (
    AvailabilitySlot,
    Booking,
    BookingStatus,
    Student,
    Tutor,
)
from app.services.booking_service import (
    check_availability,
    check_double_booking,
    create_booking_from_web,
)


# ═════════════════════════════════════════════════════════════════════
#  Helpers
# ═════════════════════════════════════════════════════════════════════


def _next_weekday(weekday: int) -> datetime:
    """Return the next occurrence of the given weekday (0=Mon) at 10:00 UTC."""
    now = datetime.now(timezone.utc)
    days_ahead = weekday - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    target = now + timedelta(days=days_ahead)
    return target.replace(hour=10, minute=0, second=0, microsecond=0)


# ═════════════════════════════════════════════════════════════════════
#  check_availability
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_availability_check_passes(seeded_session):
    """Appointment within an availability slot should pass."""
    # Monday at 10:00 — within 09:00–17:00 slot
    appt = _next_weekday(0)  # Monday
    await check_availability(
        seeded_session, tutor_id=1, appointment_time=appt,
    )
    # No exception means pass


@pytest.mark.asyncio
async def test_availability_check_wrong_day(seeded_session):
    """Appointment on a day with no slot should raise ValueError."""
    # Tuesday (1) — no slot defined
    appt = _next_weekday(1)
    with pytest.raises(ValueError, match="не принимает"):
        await check_availability(
            seeded_session, tutor_id=1, appointment_time=appt,
        )


@pytest.mark.asyncio
async def test_availability_check_wrong_time(seeded_session):
    """Appointment outside slot hours should raise ValueError."""
    # Monday at 07:00 — before 09:00 start
    appt = _next_weekday(0).replace(hour=7, minute=0)
    with pytest.raises(ValueError, match="не принимает"):
        await check_availability(
            seeded_session, tutor_id=1, appointment_time=appt,
        )


# ═════════════════════════════════════════════════════════════════════
#  check_double_booking
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_double_booking_no_conflict(seeded_session):
    """When no conflicting booking exists, check should pass."""
    appt = _next_weekday(0)
    await check_double_booking(
        seeded_session, tutor_id=1, appointment_time=appt,
    )
    # No exception means pass


@pytest.mark.asyncio
async def test_double_booking_with_conflict(seeded_session):
    """A confirmed booking within 60 min should trigger a ValueError."""
    appt = _next_weekday(0)  # Monday 10:00

    # Seed a CONFIRMED booking at 10:00
    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_type="Test",
        appointment_time=appt,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # Try to book at 10:30 — within 60-min window
    conflict_time = appt + timedelta(minutes=30)
    with pytest.raises(ValueError, match="конфликт"):
        await check_double_booking(
            seeded_session, tutor_id=1, appointment_time=conflict_time,
        )


@pytest.mark.asyncio
async def test_double_booking_outside_window(seeded_session):
    """A confirmed booking outside the 60-min window should not conflict."""
    appt = _next_weekday(0)  # Monday 10:00

    # Seed a CONFIRMED booking at 10:00
    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_type="Test",
        appointment_time=appt,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # Try to book at 11:30 — outside 60-min window
    safe_time = appt + timedelta(minutes=90)
    await check_double_booking(
        seeded_session, tutor_id=1, appointment_time=safe_time,
    )
    # No exception means pass


# ═════════════════════════════════════════════════════════════════════
#  create_booking_from_web — full integration
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_booking_success(seeded_session):
    """Happy path: booking created for a valid slot with no conflicts."""
    appt = _next_weekday(0)  # Monday 10:00

    booking = await create_booking_from_web(
        seeded_session,
        full_name="New Student",
        phone="+79001234567",
        service_type="IELTS Preparation",
        appointment_time=appt,
        tutor_id=1,
    )

    assert booking.id is not None
    assert booking.status == BookingStatus.PENDING
    assert booking.tutor_id == 1


@pytest.mark.asyncio
async def test_create_booking_rejected_no_slot(seeded_session):
    """Booking on a day without availability should be rejected."""
    appt = _next_weekday(1)  # Tuesday — no slot

    with pytest.raises(ValueError, match="не принимает"):
        await create_booking_from_web(
            seeded_session,
            full_name="New Student",
            phone="+79001234567",
            service_type="Math",
            appointment_time=appt,
            tutor_id=1,
        )


@pytest.mark.asyncio
async def test_create_booking_rejected_overlap(seeded_session):
    """Booking that overlaps a confirmed lesson should be rejected."""
    appt = _next_weekday(0)  # Monday 10:00

    # First: create and confirm a booking
    existing = Booking(
        student_id=1,
        tutor_id=1,
        service_type="Existing Lesson",
        appointment_time=appt,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(existing)
    await seeded_session.commit()

    # Try to create a new one at 10:30
    with pytest.raises(ValueError, match="конфликт"):
        await create_booking_from_web(
            seeded_session,
            full_name="Another Student",
            phone="+79009876543",
            service_type="IELTS",
            appointment_time=appt + timedelta(minutes=30),
            tutor_id=1,
        )
