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
    with pytest.raises(ValueError, match="вне рабочих часов"):
        await check_availability(
            seeded_session, tutor_id=1, appointment_time=appt,
        )


@pytest.mark.asyncio
async def test_availability_check_wrong_hour_too_late(seeded_session):
    """Appointment after slot end should raise ValueError.

    Seeded: Monday 09:00–17:00 (MSK).  23:00 UTC → 02:00+1 MSK — outside.
    """
    appt = _next_weekday(0).replace(hour=23, minute=0)
    with pytest.raises(ValueError, match="не принимает"):
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
        lesson_duration=60, buffer_time=0,
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
            lesson_duration=60, buffer_time=0,
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
            lesson_duration=60, buffer_time=0,
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
        lesson_duration=60, buffer_time=0,
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
        service_id=1,
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
            service_id=1,
            appointment_time=appt,
            tutor_id=1,
        )


@pytest.mark.asyncio
async def test_create_booking_rejected_invalid_time(seeded_session):
    """Booking at midnight should be rejected (outside slot window)."""
    appt = _next_weekday(0).replace(hour=0, minute=0)

    with pytest.raises(ValueError, match="вне рабочих часов"):
        await create_booking_from_web(
            seeded_session,
            full_name="New Student",
            phone="+79001234567",
            service_id=1,
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
            service_id=1,
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
            service_id=1,
            appointment_time=appt + timedelta(minutes=30),
            tutor_id=1,
        )


# ═════════════════════════════════════════════════════════════════════
#  Dynamic lesson_duration tests
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_double_booking_custom_short_duration(seeded_session):
    """With 45-min lesson, a booking at +50 min should NOT conflict."""
    appt = _next_weekday(0).replace(hour=10, minute=0, second=0, microsecond=0)

    from app.db.models import Service
    custom_service = Service(
        id=2, tutor_id=1, name="Short Service", duration=45, buffer_time=0, is_active=True
    )
    seeded_session.add(custom_service)
    await seeded_session.flush()

    booking = Booking(
        student_id=1, tutor_id=1, service_id=2, service_type="Test",
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
    appt = _next_weekday(0).replace(hour=10, minute=0, second=0, microsecond=0)

    from app.db.models import Service
    custom_service = Service(
        id=3, tutor_id=1, name="Buffered Service", duration=60, buffer_time=15, is_active=True
    )
    seeded_session.add(custom_service)
    await seeded_session.flush()

    booking = Booking(
        student_id=1, tutor_id=1, service_id=3, service_type="Test",
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


@pytest.mark.asyncio
async def test_reschedule_booking_same_time_fails(seeded_session):
    """Rescheduling to the exact same time should raise a ValueError."""
    appt = _next_weekday(0)  # Monday 10:00

    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test",
        appointment_time=appt,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # Try to reschedule to the same time
    with pytest.raises(ValueError, match="то же самое время"):
        await reschedule_booking(
            seeded_session,
            booking_id=booking.id,
            new_appointment_time=appt,
        )


@pytest.mark.asyncio
async def test_reschedule_booking_success(seeded_session):
    """Rescheduling to a different valid time should succeed."""
    appt = _next_weekday(0)  # Monday 10:00

    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test",
        appointment_time=appt,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # Reschedule to Wednesday 12:00 UTC (15:00 MSK - within Wednesday's 10:00-18:00 MSK availability)
    new_time = _next_weekday(2).replace(hour=12, minute=0)

    updated_booking, old_time = await reschedule_booking(
        seeded_session,
        booking_id=booking.id,
        new_appointment_time=new_time,
    )

    updated_time = updated_booking.appointment_time
    if updated_time.tzinfo is None:
        updated_time = updated_time.replace(tzinfo=timezone.utc)
    
    old_time_utc = old_time
    if old_time_utc.tzinfo is None:
        old_time_utc = old_time_utc.replace(tzinfo=timezone.utc)

    assert updated_time == new_time
    assert old_time_utc == appt


# ═════════════════════════════════════════════════════════════════════
#  Additional Coverage for Edge Cases, Absences, and Internal Booking
# ═════════════════════════════════════════════════════════════════════

from app.db.models import TutorAbsence, StudentTutorLink
from app.services.booking_service import (
    check_tutor_absence,
    get_available_slots,
    create_booking_internal,
    record_student_no_show,
)

@pytest.mark.asyncio
async def test_check_tutor_absence_raises_value_error(seeded_session):
    """If tutor absence exists for the appt time, raise ValueError."""
    appt = _next_weekday(0) # Monday 10:00 UTC
    
    absence = TutorAbsence(
        tutor_id=1,
        start_time=appt - timedelta(hours=1),
        end_time=appt + timedelta(hours=1),
        reason="Vacation"
    )
    seeded_session.add(absence)
    await seeded_session.commit()

    with pytest.raises(ValueError):
        await check_tutor_absence(seeded_session, tutor_id=1, appointment_time=appt)


@pytest.mark.asyncio
async def test_create_booking_from_web_errors(seeded_session):
    """Test various input validation errors in create_booking_from_web."""
    appt = _next_weekday(0)

    # 1. Invalid Service
    with pytest.raises(ValueError):
        await create_booking_from_web(
            seeded_session, full_name="S1", service_id=999, appointment_time=appt, tutor_id=1
        )
    await seeded_session.rollback()

    # 2. Deactivated link
    # Find link and deactivate it
    stmt = select(StudentTutorLink).where(StudentTutorLink.student_id == 1, StudentTutorLink.tutor_id == 1)
    res = await seeded_session.execute(stmt)
    link = res.scalar_one()
    link.is_active = False
    await seeded_session.commit()

    with pytest.raises(ValueError):
        await create_booking_from_web(
            seeded_session, full_name="S1", telegram_id=987654321, service_id=1, appointment_time=appt, tutor_id=1
        )
    await seeded_session.rollback()

    # Restore link
    link.is_active = True
    await seeded_session.commit()

    # 3. Tutor not found or inactive
    with pytest.raises(ValueError):
        await create_booking_from_web(
            seeded_session, full_name="S1", service_id=1, appointment_time=appt, tutor_id=999
        )
    await seeded_session.rollback()

    # Make tutor inactive
    tutor = await seeded_session.get(Tutor, 1)
    tutor.is_active = False
    await seeded_session.commit()

    with pytest.raises(ValueError):
        await create_booking_from_web(
            seeded_session, full_name="S1", service_id=1, appointment_time=appt, tutor_id=1
        )
    await seeded_session.rollback()
        
    tutor.is_active = True
    await seeded_session.commit()

    # 4. Subscription expired / not present
    tutor.subscription_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await seeded_session.commit()

    with pytest.raises(ValueError):
        await create_booking_from_web(
            seeded_session, full_name="S1", service_id=1, appointment_time=appt, tutor_id=1
        )
    await seeded_session.rollback()

    # Restore subscription
    tutor.subscription_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    await seeded_session.commit()


@pytest.mark.asyncio
async def test_create_booking_from_web_student_lookup_and_creation(seeded_session):
    """Test lookup of existing student by username, phone and fallback pseudo-phone generation."""
    appt = _next_weekday(0)

    # 1. Lookup by username
    b1 = await create_booking_from_web(
        seeded_session, full_name="Ivan", telegram_username="@ivan_test", service_id=1, appointment_time=appt, tutor_id=1
    )
    assert b1.student.telegram_username == "ivan_test"
    assert b1.student.phone.startswith("+999")

    # 2. Lookup by phone
    b2 = await create_booking_from_web(
        seeded_session, full_name="Ivan New Name", phone=b1.student.phone, service_id=1, appointment_time=appt + timedelta(hours=2), tutor_id=1
    )
    assert b2.student_id == b1.student_id
    assert b2.student.full_name == "Ivan New Name"


@pytest.mark.asyncio
async def test_get_available_slots(seeded_session):
    """Test slot generation, timezone/MSK adjustments, absences, and calendar integrations."""
    from unittest.mock import patch
    
    # Target date: next Monday
    target_date = _next_weekday(0)

    # 1. Tutor inactive or not found
    slots = await get_available_slots(seeded_session, tutor_id=999, service_id=1, date=target_date)
    assert slots == []

    # Make tutor inactive
    tutor = await seeded_session.get(Tutor, 1)
    tutor.is_active = False
    await seeded_session.commit()
    slots = await get_available_slots(seeded_session, tutor_id=1, service_id=1, date=target_date)
    assert slots == []

    tutor.is_active = True
    await seeded_session.commit()

    # 2. Expired subscription
    tutor.subscription_expires_at = None
    await seeded_session.commit()
    slots = await get_available_slots(seeded_session, tutor_id=1, service_id=1, date=target_date)
    assert slots == []

    # Restore subscription
    tutor.subscription_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    await seeded_session.commit()

    # 3. Invalid service
    slots = await get_available_slots(seeded_session, tutor_id=1, service_id=999, date=target_date)
    assert slots == []

    # 4. No slots for Tuesday
    slots_tue = await get_available_slots(seeded_session, tutor_id=1, service_id=1, date=_next_weekday(1))
    assert slots_tue == []

    # 5. Normal day (Monday), mock google calendar call to return a busy interval
    busy_start = target_date.replace(hour=11, minute=0, second=0, microsecond=0) # 11:00 UTC -> 14:00 MSK
    busy_end = busy_start + timedelta(hours=1)
    
    # Also add a TutorAbsence on Monday at 12:00 UTC (15:00 MSK)
    absence = TutorAbsence(
        tutor_id=1,
        start_time=target_date.replace(hour=12, minute=0),
        end_time=target_date.replace(hour=13, minute=0),
        reason="Doctor"
    )
    seeded_session.add(absence)
    
    # Also add an existing booking on Monday at 10:00 UTC (13:00 MSK)
    existing_booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test",
        appointment_time=target_date.replace(hour=10, minute=0),
        status=BookingStatus.CONFIRMED
    )
    seeded_session.add(existing_booking)
    await seeded_session.commit()

    # Mock calendar function
    with patch("app.services.google_calendar_service.get_busy_slots_from_calendar", return_value=[(busy_start, busy_end)]):
        slots = await get_available_slots(seeded_session, tutor_id=1, service_id=1, date=target_date)
        
        # Available times on Monday 09:00 - 17:00 MSK (12:00 - 20:00 local slot?)
        # Monday slot is seeded as 09:00-17:00 MSK.
        # MSK is UTC+3. Monday slot is 06:00 - 14:00 UTC.
        # Existing booking at 10:00 UTC (13:00 MSK) blocks 13:00-14:15 MSK (duration 60 + buffer 15 = 75m).
        # Busy calendar at 14:00 MSK blocks 14:00-15:15 MSK.
        # Tutor absence at 15:00 MSK blocks 15:00-16:15 MSK.
        # So slots at 13:00, 13:15, 13:30, 13:45, 14:00, 14:15, 14:30, 14:45, 15:00, 15:15, 15:30 should be blocked.
        # Let's verify that some times are generated and they do not contain blocked slots.
        assert len(slots) > 0
        assert "13:00" not in slots
        assert "14:00" not in slots
        assert "15:00" not in slots


@pytest.mark.asyncio
async def test_reschedule_booking_errors(seeded_session):
    """Test errors in reschedule_booking."""
    # 1. Booking not found
    with pytest.raises(ValueError):
        await reschedule_booking(seeded_session, booking_id=999, new_appointment_time=datetime.now())


@pytest.mark.asyncio
async def test_create_booking_internal(seeded_session):
    """Test create_booking_internal happy path and google calendar failure safety."""
    from unittest.mock import patch
    
    appt = _next_weekday(0) # Monday 10:00 UTC

    # 1. Invalid Service
    with pytest.raises(ValueError):
        await create_booking_internal(
            seeded_session, student_id=1, tutor_id=1, service_id=999, appointment_time=appt
        )

    # 2. Success path with mock Google Calendar exception (must be handled gracefully)
    with patch("app.services.google_calendar_service.sync_booking_to_calendar", side_effect=Exception("API Error")):
        booking = await create_booking_internal(
            seeded_session, student_id=1, tutor_id=1, service_id=1, appointment_time=appt
        )
        assert booking.id is not None
        assert booking.status == BookingStatus.CONFIRMED


@pytest.mark.asyncio
async def test_record_student_no_show(seeded_session):
    """Test no-show recording and balance deduction."""
    appt = _next_weekday(0)

    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test",
        appointment_time=appt,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    
    # Give student prepaid balance of 3
    stmt = select(StudentTutorLink).where(StudentTutorLink.student_id == 1, StudentTutorLink.tutor_id == 1)
    res = await seeded_session.execute(stmt)
    link = res.scalar_one()
    link.prepaid_balance = 3
    await seeded_session.commit()

    # 1. Record no-show (should decrement balance to 2 and set status to CANCELLED)
    updated = await record_student_no_show(seeded_session, booking_id=booking.id)
    assert updated.status == BookingStatus.CANCELLED
    
    await seeded_session.refresh(link)
    assert link.prepaid_balance == 2

    # 2. Call again on cancelled booking (should raise ValueError)
    with pytest.raises(ValueError):
        await record_student_no_show(seeded_session, booking_id=booking.id)

    # 3. Call on non-existent booking
    with pytest.raises(ValueError):
        await record_student_no_show(seeded_session, booking_id=999)


@pytest.mark.asyncio
async def test_check_double_booking_naive_datetime(seeded_session):
    """Verify check_double_booking correctly handles naive appointment times and naive DB booking times."""
    appt = _next_weekday(0).replace(tzinfo=None) # naive Monday 10:00

    # 1. Naive appt time check (should trigger line 99 replacement)
    await check_double_booking(
        seeded_session, tutor_id=1, appointment_time=appt, lesson_duration=60, buffer_time=0
    )

    # 2. Naive DB booking time check (should trigger line 105 replacement)
    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_type="Test Naive DB",
        appointment_time=appt, # naive
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    with pytest.raises(ValueError):
        await check_double_booking(
            seeded_session, tutor_id=1, appointment_time=appt + timedelta(minutes=10), lesson_duration=60, buffer_time=0
        )
    await seeded_session.rollback()


@pytest.mark.asyncio
async def test_create_booking_from_web_naive_datetime(seeded_session):
    """Test create_booking_from_web converts naive appointment_time (line 138-139)."""
    appt = _next_weekday(0).replace(tzinfo=None) # naive
    booking = await create_booking_from_web(
        seeded_session, full_name="S Naive", phone="+79008889988", service_id=1, appointment_time=appt, tutor_id=1
    )
    assert booking.appointment_time.tzinfo is not None


@pytest.mark.asyncio
async def test_create_booking_from_web_updates_existing_student_username(seeded_session):
    """Test existing student username updates during web booking (line 181)."""
    appt = _next_weekday(0)
    # Book with existing student ID 987654321 and username update
    booking = await create_booking_from_web(
        seeded_session, full_name="Ivan New Name", telegram_id=987654321, telegram_username="@new_username", service_id=1, appointment_time=appt, tutor_id=1
    )
    assert booking.student.telegram_username == "new_username"


@pytest.mark.asyncio
async def test_tutor_naive_subscription_expires(seeded_session):
    """Test naiveness of subscription_expires_at is handled in booking and slots (lines 209, 265)."""
    tutor = await seeded_session.get(Tutor, 1)
    tutor.subscription_expires_at = datetime.now() + timedelta(days=5) # naive
    await seeded_session.commit()

    # 1. Create booking (line 209)
    appt = _next_weekday(0)
    booking = await create_booking_from_web(
        seeded_session, full_name="Sub Test", phone="+79007776655", service_id=1, appointment_time=appt, tutor_id=1
    )
    assert booking.id is not None

    # 2. Get available slots (line 265)
    slots = await get_available_slots(seeded_session, tutor_id=1, service_id=1, date=_next_weekday(0))
    assert len(slots) > 0


@pytest.mark.asyncio
async def test_get_available_slots_gcal_exception(seeded_session):
    """Test get_available_slots when Google Calendar API throws an error (lines 316-318)."""
    from unittest.mock import patch
    with patch("app.services.google_calendar_service.get_busy_slots_from_calendar", side_effect=Exception("API Mismatch")):
        slots = await get_available_slots(seeded_session, tutor_id=1, service_id=1, date=_next_weekday(0))
        assert len(slots) > 0


@pytest.mark.asyncio
async def test_get_available_slots_today_shifting(seeded_session):
    """Test slots shifting logic for today's queries (lines 327-330)."""
    from app.bot.formatting import MSK
    
    # We query availability slots for TODAY
    today = datetime.now(timezone.utc)
    weekday = today.astimezone(MSK).weekday()
    
    # Add an availability slot for today's weekday
    slot = AvailabilitySlot(
        tutor_id=1,
        weekday=weekday,
        start_time=time(1, 0),
        end_time=time(23, 0)
    )
    seeded_session.add(slot)
    await seeded_session.commit()
    
    slots = await get_available_slots(seeded_session, tutor_id=1, service_id=1, date=today)
    assert isinstance(slots, list)


@pytest.mark.asyncio
async def test_get_available_slots_naive_booking_and_absence(seeded_session):
    """Test naive DB booking and absence handling inside get_available_slots (lines 342, 355, 358)."""
    target_date = _next_weekday(0)
    
    # Naive booking
    existing_booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Naive DB",
        appointment_time=target_date.replace(hour=10, minute=0, tzinfo=None), # naive
        status=BookingStatus.CONFIRMED
    )
    seeded_session.add(existing_booking)

    # Naive absence
    absence = TutorAbsence(
        tutor_id=1,
        start_time=target_date.replace(hour=12, minute=0, tzinfo=None), # naive
        end_time=target_date.replace(hour=13, minute=0, tzinfo=None), # naive
        reason="Doctor"
    )
    seeded_session.add(absence)
    await seeded_session.commit()

    slots = await get_available_slots(seeded_session, tutor_id=1, service_id=1, date=target_date)
    assert len(slots) > 0


@pytest.mark.asyncio
async def test_reschedule_booking_naive_datetime(seeded_session):
    """Test reschedule_booking handles naive inputs and DB values correctly (lines 386, 401)."""
    appt = _next_weekday(0)
    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test",
        appointment_time=appt.replace(tzinfo=None), # naive DB booking time (line 401)
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # Naive new appointment time (line 386)
    new_time = _next_weekday(2).replace(hour=12, minute=0, tzinfo=None) # naive

    updated_booking, old_time = await reschedule_booking(
        seeded_session,
        booking_id=booking.id,
        new_appointment_time=new_time,
    )
    assert updated_booking.appointment_time.tzinfo is not None


@pytest.mark.asyncio
async def test_create_booking_internal_naive_datetime(seeded_session):
    """Test create_booking_internal converts naive appointment_time (line 443)."""
    appt = _next_weekday(0).replace(tzinfo=None) # naive
    booking = await create_booking_internal(
        seeded_session, student_id=1, tutor_id=1, service_id=1, appointment_time=appt
    )
    assert booking.appointment_time.replace(tzinfo=None) == appt.replace(tzinfo=None)




