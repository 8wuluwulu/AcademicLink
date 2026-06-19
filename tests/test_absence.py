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
            service_id=1,
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


@pytest.mark.asyncio
async def test_cb_quick_block_today_no_slots(seeded_session):
    """Test cb_quick_block_today when no slots are defined today (fixing UnboundLocalError)."""
    from app.bot.handlers import cb_quick_block_today
    from app.db.models import Tutor, AvailabilitySlot
    from unittest.mock import AsyncMock, patch
    
    # Get a tutor
    result = await seeded_session.execute(select(Tutor).limit(1))
    tutor = result.scalar_one()
    
    # Delete all their availability slots for today so last_slot is None
    # Let's get today's weekday
    from app.bot.formatting import MSK
    now_local = datetime.now(MSK)
    today_weekday = now_local.weekday()
    
    # Delete availability slots for this weekday
    from sqlalchemy import delete
    await seeded_session.execute(
        delete(AvailabilitySlot).where(
            AvailabilitySlot.tutor_id == tutor.id,
            AvailabilitySlot.weekday == today_weekday
        )
    )
    await seeded_session.commit()
    
    # Mock CallbackQuery
    callback = AsyncMock()
    callback.from_user.id = tutor.tg_id
    callback.message = AsyncMock()
    callback.answer = AsyncMock()
    
    # MockAsyncSessionContext class
    class MockAsyncSessionContext:
        def __init__(self, sess):
            self.sess = sess
        async def __aenter__(self):
            return self.sess
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    # Patch session factory and show_absence_manager
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(seeded_session)), \
         patch("app.bot.handlers._show_absence_manager") as mock_show_absence:
         
        await cb_quick_block_today(callback)
        
        # Verify absence is created
        result = await seeded_session.execute(
            select(TutorAbsence).where(TutorAbsence.tutor_id == tutor.id)
        )
        absences = result.scalars().all()
        assert len(absences) >= 1
        
        # Verify quick block message was sent successfully
        callback.message.answer.assert_called_once()
        sent_text = callback.message.answer.call_args[0][0]
        assert "⚡️ <b>Время занято!</b>" in sent_text
        assert "до 23:59" in sent_text
        
        # Verify callback.answer was called
        callback.answer.assert_called_once()

