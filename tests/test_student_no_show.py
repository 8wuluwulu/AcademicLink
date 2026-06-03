"""
AcademicLink — Unit Tests for Student Absence / No-show
"""

import pytest
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.db.models import Booking, BookingStatus, Student
from app.services.booking_service import record_student_no_show

@pytest.mark.asyncio
async def test_student_no_show_deducts_prepaid_balance(seeded_session):
    """
    If a student has a prepaid balance, marking a booking as a no-show 
    should deduct one lesson and cancel the booking.
    """
    # Find the seeded student and give them a prepaid balance of 3
    result = await seeded_session.execute(select(Student).limit(1))
    student = result.scalar_one()
    student.prepaid_balance = 3
    await seeded_session.commit()

    # Create a confirmed booking
    booking = Booking(
        student_id=student.id,
        tutor_id=1,
        service_id=1,
        service_type="Test Service",
        appointment_time=datetime.now(timezone.utc) + timedelta(days=1),
        status=BookingStatus.CONFIRMED
    )
    seeded_session.add(booking)
    await seeded_session.commit()
    await seeded_session.refresh(booking)

    # Record student no-show
    updated_booking = await record_student_no_show(seeded_session, booking_id=booking.id)

    # Check status and remaining prepaid balance
    assert updated_booking.status == BookingStatus.CANCELLED
    assert student.prepaid_balance == 2


@pytest.mark.asyncio
async def test_student_no_show_without_prepaid_balance(seeded_session):
    """
    If a student has 0 prepaid balance, marking as a no-show 
    cancels the booking but doesn't throw or go negative.
    """
    result = await seeded_session.execute(select(Student).limit(1))
    student = result.scalar_one()
    student.prepaid_balance = 0
    await seeded_session.commit()

    # Create a confirmed booking
    booking = Booking(
        student_id=student.id,
        tutor_id=1,
        service_id=1,
        service_type="Test Service",
        appointment_time=datetime.now(timezone.utc) + timedelta(days=1),
        status=BookingStatus.CONFIRMED
    )
    seeded_session.add(booking)
    await seeded_session.commit()
    await seeded_session.refresh(booking)

    # Record student no-show
    updated_booking = await record_student_no_show(seeded_session, booking_id=booking.id)

    # Check status and remaining prepaid balance
    assert updated_booking.status == BookingStatus.CANCELLED
    assert student.prepaid_balance == 0
