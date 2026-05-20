"""
AcademicLink — Unit Tests for Scheduler
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from app.core.scheduler import pre_lesson_reminder_job
from app.db.models import Booking, BookingStatus
from app.core.config import settings

@pytest.mark.asyncio
async def test_pre_lesson_reminder_job_picks_correct_bookings(seeded_session):
    """Verify that reminders are sent for bookings in the lead-time window."""
    
    # 100 minutes from now
    lead_time = settings.reminder_minutes_before
    appt_time = datetime.now(timezone.utc) + timedelta(minutes=lead_time)
    
    # Create a confirmed booking in the window
    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_type="Reminder Test",
        appointment_time=appt_time,
        status=BookingStatus.CONFIRMED
    )
    seeded_session.add(booking)
    await seeded_session.commit()
    
    # Mock bot
    mock_bot = AsyncMock()
    
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            await pre_lesson_reminder_job()
            
    # Check if bot.send_message was called
    # (One for tutor, one for student if linked. Seeded student has telegram_id=987654321)
    assert mock_bot.send_message.call_count >= 1
    
    # Verify booking marked as reminded
    await seeded_session.refresh(booking)
    assert booking.reminded_at is not None

@pytest.mark.asyncio
async def test_pre_lesson_reminder_job_ignores_too_early_or_late(seeded_session):
    """Reminders should NOT be sent for bookings outside the lead-time window."""
    
    lead_time = settings.reminder_minutes_before
    
    # Too early (e.g. 50 minutes from now)
    early_appt = datetime.now(timezone.utc) + timedelta(minutes=lead_time - 50)
    # Too late (e.g. 150 minutes from now)
    late_appt = datetime.now(timezone.utc) + timedelta(minutes=lead_time + 50)
    
    b1 = Booking(student_id=1, tutor_id=1, service_type="Early", appointment_time=early_appt, status=BookingStatus.CONFIRMED)
    b2 = Booking(student_id=1, tutor_id=1, service_type="Late", appointment_time=late_appt, status=BookingStatus.CONFIRMED)
    seeded_session.add_all([b1, b2])
    await seeded_session.commit()
    
    mock_bot = AsyncMock()
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            await pre_lesson_reminder_job()
            
    assert mock_bot.send_message.call_count == 0
