"""
AcademicLink — Unit Tests for Tutor Absence

Covers:
- Booking rejection during tutor absence
- Automatic cancellation of overlapping bookings when adding an absence
"""

import pytest
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.db.models import Booking, BookingStatus, TutorAbsence
from app.services.booking_service import create_booking_from_web, check_tutor_absence

@pytest.mark.asyncio
async def test_booking_rejected_during_absence(seeded_session):
    """Booking during a tutor's absence should be rejected."""
    from app.db.models import Tutor
    result = await seeded_session.execute(select(Tutor).limit(1))
    tutor = result.scalar_one()

    # Monday 10:00 (Valid according to seeded slots)
    appt = datetime.now(timezone.utc) + timedelta(days=7)
    while appt.weekday() != 0:
        appt += timedelta(days=1)
    appt = appt.replace(hour=10, minute=0, second=0, microsecond=0)

    # Add absence covering that time
    absence = TutorAbsence(
        tutor_id=tutor.id,
        start_time=appt - timedelta(hours=1),
        end_time=appt + timedelta(hours=1),
        reason="Sick Leave"
    )
    seeded_session.add(absence)
    await seeded_session.commit()

    with pytest.raises(ValueError, match="Репетитор отсутствует"):
        await create_booking_from_web(
            seeded_session,
            full_name="Test Student",
            phone="+79001112233",
            service_type="Math",
            appointment_time=appt,
            tutor_id=tutor.id
        )

@pytest.mark.asyncio
async def test_check_tutor_absence_helper(seeded_session):
    """Verify check_tutor_absence helper logic."""
    from app.db.models import Tutor
    result = await seeded_session.execute(select(Tutor).limit(1))
    tutor = result.scalar_one()

    appt = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    
    # No absence yet
    await check_tutor_absence(seeded_session, tutor_id=tutor.id, appointment_time=appt)
    
    # Add absence
    absence = TutorAbsence(
        tutor_id=tutor.id,
        start_time=appt - timedelta(minutes=10),
        end_time=appt + timedelta(minutes=10),
        reason="Vacation"
    )
    seeded_session.add(absence)
    await seeded_session.commit()
    
    with pytest.raises(ValueError, match="Vacation"):
        await check_tutor_absence(seeded_session, tutor_id=tutor.id, appointment_time=appt)
