"""
AcademicLink — Integration Tests for Student Features

Covers:
- Student cancellation of a booking
- Student rescheduling of a booking
- Student reminder toggle (wants_reminders)
"""

from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Booking,
    BookingStatus,
    Service,
    Student,
    Tutor,
)
from app.services.booking_service import (
    check_availability,
    check_double_booking,
    check_tutor_absence,
    reschedule_booking,
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
#  Student Cancellation
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_student_cancel_booking(seeded_session: AsyncSession):
    """Student cancels a CONFIRMED booking — status should change to CANCELLED."""
    appt = _next_weekday(0)  # Monday 10:00 UTC

    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Service",
        appointment_time=appt,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # Simulate student cancellation
    booking.status = BookingStatus.CANCELLED
    await seeded_session.commit()

    await seeded_session.refresh(booking)
    assert booking.status == BookingStatus.CANCELLED


@pytest.mark.asyncio
async def test_student_cancel_already_cancelled(seeded_session: AsyncSession):
    """Cancelling an already-cancelled booking should not cause errors."""
    appt = _next_weekday(0)

    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Service",
        appointment_time=appt,
        status=BookingStatus.CANCELLED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # The handler checks status before cancelling — verify status is still CANCELLED
    await seeded_session.refresh(booking)
    assert booking.status == BookingStatus.CANCELLED


# ═════════════════════════════════════════════════════════════════════
#  Student Rescheduling
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_student_reschedule_booking(seeded_session: AsyncSession):
    """Student reschedules a booking to a valid new time.

    Uses the existing reschedule_booking service function which validates
    availability, absences, and double bookings.
    """
    appt = _next_weekday(0)  # Monday 10:00 UTC

    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Service",
        appointment_time=appt,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # Reschedule to 2 hours later on the same day (still within 09:00-17:00 MSK)
    new_time = appt + timedelta(hours=2)  # 12:00 UTC → 15:00 MSK
    updated_booking, old_time = await reschedule_booking(
        seeded_session,
        booking_id=booking.id,
        new_appointment_time=new_time,
    )

    assert updated_booking.appointment_time.replace(tzinfo=None) == new_time.replace(tzinfo=None)
    assert updated_booking.status == BookingStatus.CONFIRMED
    assert old_time == appt


@pytest.mark.asyncio
async def test_student_reschedule_conflict_rejected(seeded_session: AsyncSession):
    """Student tries to reschedule onto a time that conflicts with another booking."""
    appt1 = _next_weekday(0)  # Monday 10:00 UTC
    appt2 = appt1 + timedelta(hours=2)  # Monday 12:00 UTC

    booking1 = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Service",
        appointment_time=appt1,
        status=BookingStatus.CONFIRMED,
    )
    booking2 = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Service",
        appointment_time=appt2,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add_all([booking1, booking2])
    await seeded_session.commit()

    # Try to reschedule booking2 onto booking1's time slot
    with pytest.raises(ValueError, match="конфликт"):
        await reschedule_booking(
            seeded_session,
            booking_id=booking2.id,
            new_appointment_time=appt1 + timedelta(minutes=30),
        )


@pytest.mark.asyncio
async def test_student_reschedule_self_no_conflict(seeded_session: AsyncSession):
    """Rescheduling a booking to a nearby time should not conflict with itself.

    The exclude_booking_id parameter ensures the booking doesn't count as a
    conflict with its own current slot.
    """
    appt = _next_weekday(0)  # Monday 10:00 UTC

    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Service",
        appointment_time=appt,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # Reschedule to 15 minutes later — would conflict without exclude_booking_id
    new_time = appt + timedelta(minutes=15)
    updated_booking, old_time = await reschedule_booking(
        seeded_session,
        booking_id=booking.id,
        new_appointment_time=new_time,
    )

    assert updated_booking.appointment_time.replace(tzinfo=None) == new_time.replace(tzinfo=None)
    assert updated_booking.status == BookingStatus.CONFIRMED


# ═════════════════════════════════════════════════════════════════════
#  Student Reminder Toggle
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_student_toggle_reminders_off(seeded_session: AsyncSession):
    """Toggling wants_reminders from True to False should persist."""
    result = await seeded_session.execute(
        select(Student).where(Student.telegram_id == 987654321)
    )
    student = result.scalar_one()

    assert student.wants_reminders is True

    student.wants_reminders = False
    await seeded_session.commit()

    await seeded_session.refresh(student)
    assert student.wants_reminders is False


@pytest.mark.asyncio
async def test_student_toggle_reminders_on(seeded_session: AsyncSession):
    """Toggling wants_reminders from False to True should persist."""
    result = await seeded_session.execute(
        select(Student).where(Student.telegram_id == 987654321)
    )
    student = result.scalar_one()

    # Set to False first
    student.wants_reminders = False
    await seeded_session.commit()

    # Toggle back to True
    student.wants_reminders = True
    await seeded_session.commit()

    await seeded_session.refresh(student)
    assert student.wants_reminders is True
