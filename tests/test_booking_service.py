"""
AcademicLink — Unit Tests for Booking Service

Covers:
- Successful booking creation
- Availability slot rejection (DB-driven)
- Double-booking / overlap rejection (PENDING + CONFIRMED)
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
#  check_availability (DB-driven)
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_availability_check_passes(seeded_session):
    """Appointment within a seeded slot should pass.

    Seeded: Monday 09:00–17:00.  10:00 UTC → 13:00 MSK.
    """
    appt = _next_weekday(0)  # Monday 10:00 UTC → 13:00 MSK
    await check_availability(
        seeded_session, tutor_id=1, appointment_time=appt,
    )
    # No exception means pass


@pytest.mark.asyncio
async def test_availability_check_no_slot_for_day(seeded_session):
    """Booking on a day with no AvailabilitySlot should be rejected.

    Seeded slots: Mon, Wed, Fri only.  Tuesday has no slot.
    """
    appt = _next_weekday(1)  # Tuesday 10:00 UTC
    with pytest.raises(ValueError, match="не принимает в этот день"):
        await check_availability(
            seeded_session, tutor_id=1, appointment_time=appt,
        )


@pytest.mark.asyncio
async def test_availability_check_wrong_hour_too_early(seeded_session):
    """Appointment outside slot window should raise ValueError.

    Seeded: Monday 09:00–17:00 (MSK).  02:00 UTC → 05:00 MSK — outside.
    """
    appt = _next_weekday(0).replace(hour=2, minute=0)
    with pytest.raises(ValueError, match="не принимает в"):
        await check_availability(
            seeded_session, tutor_id=1, appointment_time=appt,
        )


@pytest.mark.asyncio
async def test_availability_check_wrong_hour_too_late(seeded_session):
    """Appointment after slot end should raise ValueError.

    Seeded: Monday 09:00–17:00 (MSK).  23:00 UTC → 02:00+1 MSK — outside.
    """
    appt = _next_weekday(0).replace(hour=23, minute=0)
    with pytest.raises(ValueError, match="не принимает в"):
        await check_availability(
            seeded_session, tutor_id=1, appointment_time=appt,
        )


# ═════════════════════════════════════════════════════════════════════
#  check_double_booking (CONFIRMED + PENDING)
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
async def test_double_booking_with_confirmed_conflict(seeded_session):
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
async def test_double_booking_with_pending_conflict(seeded_session):
    """A PENDING booking within 60 min should ALSO trigger a ValueError.

    This is the core overbooking prevention test — previously only
    CONFIRMED bookings were checked.
    """
    appt = _next_weekday(0)  # Monday 10:00

    # Seed a PENDING booking at 10:00
    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_type="Test",
        appointment_time=appt,
        status=BookingStatus.PENDING,
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
    """Booking on a day with no availability slot should be rejected."""
    appt = _next_weekday(1)  # Tuesday — no slot seeded

    with pytest.raises(ValueError, match="не принимает в этот день"):
        await create_booking_from_web(
            seeded_session,
            full_name="New Student",
            phone="+79001234567",
            service_type="Math",
            appointment_time=appt,
            tutor_id=1,
        )


@pytest.mark.asyncio
async def test_create_booking_rejected_invalid_time(seeded_session):
    """Booking at midnight should be rejected (outside slot window)."""
    appt = _next_weekday(0).replace(hour=0, minute=0)

    with pytest.raises(ValueError, match="не принимает в"):
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


@pytest.mark.asyncio
async def test_create_booking_rejected_pending_overlap(seeded_session):
    """Booking that overlaps a PENDING booking should also be rejected.

    This is the critical overbooking regression test.
    """
    appt = _next_weekday(0)  # Monday 10:00

    # First: create a PENDING booking (not confirmed yet)
    existing = Booking(
        student_id=1,
        tutor_id=1,
        service_type="Pending Lesson",
        appointment_time=appt,
        status=BookingStatus.PENDING,
    )
    seeded_session.add(existing)
    await seeded_session.commit()

    # Try to create a new one at 10:30 — should be blocked
    with pytest.raises(ValueError, match="конфликт"):
        await create_booking_from_web(
            seeded_session,
            full_name="Another Student",
            phone="+79009876543",
            service_type="IELTS",
            appointment_time=appt + timedelta(minutes=30),
            tutor_id=1,
        )


# ═════════════════════════════════════════════════════════════════════
#  Dynamic lesson_duration tests
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_double_booking_custom_short_duration(seeded_session):
    """With 45-min lesson, a booking at +50 min should NOT conflict."""
    appt = _next_weekday(0)  # Monday 10:00

    booking = Booking(
        student_id=1, tutor_id=1, service_type="Test",
        appointment_time=appt, status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # 50 min later — outside the 45-min window
    safe_time = appt + timedelta(minutes=50)
    await check_double_booking(
        seeded_session, tutor_id=1, appointment_time=safe_time,
        lesson_duration=45, buffer_time=0,
    )
    # No exception means pass


@pytest.mark.asyncio
async def test_double_booking_custom_duration_with_buffer(seeded_session):
    """With 60-min lesson + 15-min buffer (total 75), a booking at +60 min should conflict."""
    appt = _next_weekday(0)  # Monday 10:00

    booking = Booking(
        student_id=1, tutor_id=1, service_type="Test",
        appointment_time=appt, status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # 60 min later — within the 75-min window (60 + 15 buffer)
    conflict_time = appt + timedelta(minutes=60)
    with pytest.raises(ValueError, match="конфликт"):
        await check_double_booking(
            seeded_session, tutor_id=1, appointment_time=conflict_time,
            lesson_duration=60, buffer_time=15,
        )
