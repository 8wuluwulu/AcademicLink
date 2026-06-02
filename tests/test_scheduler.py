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


@pytest.mark.asyncio
async def test_subscription_renewal_alert_job(seeded_session):
    """Verify that renewal alerts are sent only to tutors whose sub expires in 1 or 3 days."""
    from app.core.scheduler import subscription_renewal_alert_job
    from app.db.models import Tutor
    
    now = datetime.now(timezone.utc)
    
    # Tutor 1: expires in exactly 3 days (e.g. 70 hours from now)
    t1 = Tutor(
        tg_id=111,
        name="Tutor 3 Days",
        is_active=True,
        subscription_expires_at=now + timedelta(hours=70),
        subscription_status="active"
    )
    
    # Tutor 2: expires in exactly 1 day (e.g. 20 hours from now)
    t2 = Tutor(
        tg_id=222,
        name="Tutor 1 Day",
        is_active=True,
        subscription_expires_at=now + timedelta(hours=20),
        subscription_status="active"
    )
    
    # Tutor 3: expires in 10 days (no alert)
    t3 = Tutor(
        tg_id=333,
        name="Tutor 10 Days",
        is_active=True,
        subscription_expires_at=now + timedelta(days=10),
        subscription_status="active"
    )
    
    seeded_session.add_all([t1, t2, t3])
    await seeded_session.commit()
    
    mock_bot = AsyncMock()
    
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            await subscription_renewal_alert_job()
            
    # Check if bot.send_message was called exactly twice (once for t1, once for t2)
    assert mock_bot.send_message.call_count == 2
    
    # Check the call arguments
    called_tg_ids = [call.kwargs["chat_id"] for call in mock_bot.send_message.call_args_list]
    assert 111 in called_tg_ids
    assert 222 in called_tg_ids
    assert 333 not in called_tg_ids
    
    # Check messages contain correct days
    for call in mock_bot.send_message.call_args_list:
        chat_id = call.kwargs["chat_id"]
        text = call.kwargs["text"]
        if chat_id == 111:
            assert "истекает через 3 дня" in text
        elif chat_id == 222:
            assert "истекает через 1 день" in text

