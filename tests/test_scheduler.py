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


@pytest.mark.asyncio
async def test_sync_google_calendar_changes_job(seeded_session):
    """Verify that Google Calendar changes (rescheduling & deletion) are correctly synced."""
    from app.core.scheduler import sync_google_calendar_changes_job
    from app.db.models import Tutor, Booking, BookingStatus, Student
    
    tutor = Tutor(
        tg_id=444,
        name="Tutor Calendar Test",
        is_active=True,
        google_token_json='{"token": "dummy"}'
    )
    student = Student(
        full_name="Student Calendar Test",
        phone="+79991112233",
        telegram_id=555
    )
    seeded_session.add_all([tutor, student])
    await seeded_session.flush()
    
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    b1_time = now + timedelta(days=5)
    b2_time = now + timedelta(days=6)
    
    b_resched = Booking(
        student_id=student.id,
        tutor_id=tutor.id,
        service_id=1,
        service_type="Math",
        appointment_time=b1_time,
        status=BookingStatus.CONFIRMED,
        google_event_id="event_resched_123"
    )
    
    b_cancel = Booking(
        student_id=student.id,
        tutor_id=tutor.id,
        service_id=1,
        service_type="Physics",
        appointment_time=b2_time,
        status=BookingStatus.CONFIRMED,
        google_event_id="event_cancel_456"
    )
    
    seeded_session.add_all([b_resched, b_cancel])
    await seeded_session.commit()
    
    new_b1_time = b1_time + timedelta(hours=2)
    from unittest.mock import MagicMock
    mock_events_response = MagicMock()
    mock_events_response.status_code = 200
    mock_events_response.json.return_value = {
        "items": [
            {
                "id": "event_resched_123",
                "status": "confirmed",
                "start": {"dateTime": new_b1_time.isoformat()}
            },
            {
                "id": "event_cancel_456",
                "status": "cancelled"
            }
        ]
    }
    
    mock_bot = AsyncMock()
    
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_events_response):
            with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
                await sync_google_calendar_changes_job()
                
    await seeded_session.refresh(b_resched)
    await seeded_session.refresh(b_cancel)
    
    assert b_resched.appointment_time.replace(tzinfo=None) == new_b1_time.replace(tzinfo=None)
    assert b_cancel.status == BookingStatus.CANCELLED
    assert b_cancel.google_event_id is None
    assert mock_bot.send_message.call_count >= 2


@pytest.mark.asyncio
async def test_pending_bookings_reminder_job(seeded_session):
    """Verify that pending booking reminders are sent to tutors with pending bookings."""
    from app.core.scheduler import pending_bookings_reminder_job
    from app.db.models import Tutor, Booking, BookingStatus, Student
    
    tutor1 = Tutor(tg_id=777, name="Tutor Pending 1", is_active=True)
    tutor2 = Tutor(tg_id=888, name="Tutor Pending 2", is_active=True)
    student = Student(full_name="Student Test", phone="+79991112244", telegram_id=666)
    seeded_session.add_all([tutor1, tutor2, student])
    await seeded_session.flush()
    
    # Tutor 1 has a pending booking
    b1 = Booking(
        student_id=student.id,
        tutor_id=tutor1.id,
        service_type="Math",
        appointment_time=datetime.now(timezone.utc) + timedelta(days=1),
        status=BookingStatus.PENDING
    )
    # Tutor 2 has only confirmed booking (no pending)
    b2 = Booking(
        student_id=student.id,
        tutor_id=tutor2.id,
        service_type="Physics",
        appointment_time=datetime.now(timezone.utc) + timedelta(days=2),
        status=BookingStatus.CONFIRMED
    )
    seeded_session.add_all([b1, b2])
    await seeded_session.commit()
    
    mock_bot = AsyncMock()
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            await pending_bookings_reminder_job()
            
    # Should call send_message only for tutor1
    assert mock_bot.send_message.call_count == 1
    called_chat_ids = [call.kwargs["chat_id"] for call in mock_bot.send_message.call_args_list]
    assert 777 in called_chat_ids
    assert 888 not in called_chat_ids
    assert "неподтвержденные заявки" in mock_bot.send_message.call_args.kwargs["text"]


# ═════════════════════════════════════════════════════════════════════
#  Additional Scheduler Coverage: Exception Handling and Edge Cases
# ═════════════════════════════════════════════════════════════════════

from unittest.mock import MagicMock

from app.core.scheduler import (
    daily_reminder_job,
    subscription_renewal_alert_job,
    pending_bookings_reminder_job,
    configure_scheduler,
    scheduler
)
from app.db.models import Tutor, Booking, BookingStatus, Student

@pytest.mark.asyncio
async def test_scheduler_jobs_when_bot_is_none(seeded_session):
    """Verify that jobs return immediately without failing when bot is not initialized."""
    with patch("app.core.scheduler._get_bot", return_value=None):
        # 1. Pre lesson
        await pre_lesson_reminder_job()
        # 2. Daily
        await daily_reminder_job()
        # 3. Sub renewal
        await subscription_renewal_alert_job()
        # 4. Pending reminder
        await pending_bookings_reminder_job()
        # No errors means pass


@pytest.mark.asyncio
async def test_reminders_bot_sending_exceptions(seeded_session):
    """Test exception handling in pre-lesson and daily reminder jobs when bot fails to send a message."""
    lead_time = settings.reminder_minutes_before
    appt_time = datetime.now(timezone.utc) + timedelta(minutes=lead_time)
    
    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_type="Reminder Test",
        appointment_time=appt_time,
        status=BookingStatus.CONFIRMED
    )
    seeded_session.add(booking)
    await seeded_session.commit()
    
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = Exception("TG Send Failed")
    
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            # 1. Test pre-lesson failure path (lines 107, 134)
            await pre_lesson_reminder_job()
            
            # Reset and set 24h booking
            booking.reminded_at = None
            booking.reminded_24h_at = None
            booking.appointment_time = datetime.now(timezone.utc) + timedelta(hours=24)
            await seeded_session.commit()
            
            # 2. Test daily failure path (lines 214, 240)
            await daily_reminder_job()


@pytest.mark.asyncio
async def test_subscription_renewal_alert_warned_dedup(seeded_session):
    """Verify subscription renewal skips alert if already warned in the last 20 hours (line 301)."""
    now = datetime.now(timezone.utc)
    t = Tutor(
        tg_id=99911,
        name="Warned Tutor",
        is_active=True,
        subscription_expires_at=now + timedelta(hours=20),
        subscription_warned_at=now - timedelta(hours=5), # Warned 5 hours ago
        subscription_status="active"
    )
    seeded_session.add(t)
    await seeded_session.commit()

    mock_bot = AsyncMock()
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            await subscription_renewal_alert_job()
    
    # Alert should NOT be sent because warned_at is less than 20 hours ago
    assert mock_bot.send_message.call_count == 0


@pytest.mark.asyncio
async def test_subscription_renewal_alert_bot_fails(seeded_session):
    """Verify sub renewal warning exception handling when bot sending fails (lines 314-315)."""
    now = datetime.now(timezone.utc)
    t = Tutor(
        tg_id=99922,
        name="Tutor Expiring",
        is_active=True,
        subscription_expires_at=now + timedelta(hours=20),
        subscription_status="active"
    )
    seeded_session.add(t)
    await seeded_session.commit()

    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = Exception("Alert failed")
    
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            await subscription_renewal_alert_job()
            
    # Exception is logged and tutor is NOT marked as warned
    await seeded_session.refresh(t)
    assert t.subscription_warned_at is None


@pytest.mark.asyncio
async def test_sync_google_calendar_changes_errors(seeded_session):
    """Test error handling in Google Calendar sync job for bad responses, missing event fields, and TG fails."""
    from app.core.scheduler import sync_google_calendar_changes_job
    
    tutor = Tutor(
        tg_id=44400,
        name="Tutor Calendar Test",
        is_active=True,
        google_token_json='{"token": "dummy"}'
    )
    seeded_session.add(tutor)
    await seeded_session.flush()

    # 1. Mock GET Calendar events returning 400 Bad Request
    mock_events_bad = MagicMock()
    mock_events_bad.status_code = 400
    mock_events_bad.text = "Bad Auth"

    mock_bot = AsyncMock()
    
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_events_bad):
            with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
                await sync_google_calendar_changes_job()
                # Should log error and continue

    # 2. Mock GET returning events with missing id or date parse errors
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    
    # We must seed bookings for these google_event_ids so that we pass the 'if not booking' check
    b_bad_time = Booking(
        student_id=1,
        tutor_id=tutor.id,
        service_id=1,
        service_type="Math",
        appointment_time=now + timedelta(days=2),
        status=BookingStatus.CONFIRMED,
        google_event_id="event_bad_time_123"
    )
    b_no_start = Booking(
        student_id=1,
        tutor_id=tutor.id,
        service_id=1,
        service_type="Math",
        appointment_time=now + timedelta(days=3),
        status=BookingStatus.CONFIRMED,
        google_event_id="event_no_start_456"
    )
    seeded_session.add_all([b_bad_time, b_no_start])
    await seeded_session.commit()

    mock_events_bad_fields = MagicMock()
    mock_events_bad_fields.status_code = 200
    mock_events_bad_fields.json.return_value = {
        "items": [
            {"status": "confirmed"}, # missing id (line 363)
            {"id": "non_existent_event_id_999", "status": "confirmed", "start": {"dateTime": now.isoformat()}}, # no matching booking (line 376)
            {"id": "event_bad_time_123", "status": "confirmed", "start": {"dateTime": "not-a-date"}}, # parse error (line 431)
            {"id": "event_no_start_456", "status": "confirmed", "start": {}} # missing start (line 423)
        ]
    }
    
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_events_bad_fields):
            with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
                await sync_google_calendar_changes_job()

    # 3. Test bot send message failure on cancellation and reschedule
    b_cancel = Booking(
        student_id=1,
        tutor_id=tutor.id,
        service_id=1,
        service_type="Math",
        appointment_time=now + timedelta(days=2),
        status=BookingStatus.CONFIRMED,
        google_event_id="event_tg_fail_cancel"
    )
    b_resched = Booking(
        student_id=1,
        tutor_id=tutor.id,
        service_id=1,
        service_type="Math",
        appointment_time=(now + timedelta(days=3)).replace(tzinfo=None), # naive DB booking time (line 438)
        status=BookingStatus.CONFIRMED,
        google_event_id="event_tg_fail_resched"
    )
    seeded_session.add_all([b_cancel, b_resched])
    await seeded_session.commit()

    mock_events_ok = MagicMock()
    mock_events_ok.status_code = 200
    mock_events_ok.json.return_value = {
        "items": [
            {"id": "event_tg_fail_cancel", "status": "cancelled"},
            {"id": "event_tg_fail_resched", "status": "confirmed", "start": {"dateTime": (now + timedelta(days=3, hours=1)).isoformat()}}
        ]
    }

    mock_bot_fail = AsyncMock()
    mock_bot_fail.send_message.side_effect = Exception("TG Down")

    with patch("app.core.scheduler._get_bot", return_value=mock_bot_fail):
        with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_events_ok):
            with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
                await sync_google_calendar_changes_job()
                # Should complete without throwing exceptions


def test_configure_scheduler():
    """Verify that configure_scheduler registers the 5 required background jobs."""
    configure_scheduler()
    
    # Get registered job IDs
    job_ids = [job.id for job in scheduler.get_jobs()]
    
    assert "pre_lesson_reminders" in job_ids
    assert "daily_reminders" in job_ids
    assert "subscription_renewal_alerts" in job_ids
    assert "sync_google_calendar_changes" in job_ids
    assert "pending_bookings_reminders" in job_ids


def test_get_bot_directly():
    """Directly call _get_bot to cover line 37-38."""
    from app.core.scheduler import _get_bot
    # Should run fine, returns None when bot is not mocked or initialized
    result = _get_bot()
    assert result is None or hasattr(result, "send_message")


@pytest.mark.asyncio
async def test_subscription_renewal_alert_naive_datetimes(seeded_session):
    """Verify subscription renewal alert job converts naive datetimes properly (lines 284, 300)."""
    now = datetime.now()
    t = Tutor(
        tg_id=99933,
        name="Naive tutor",
        is_active=True,
        subscription_expires_at=now + timedelta(hours=20), # naive expiration (line 284)
        subscription_warned_at=now - timedelta(hours=5), # naive warned (line 300)
        subscription_status="active"
    )
    seeded_session.add(t)
    await seeded_session.commit()
    
    mock_bot = AsyncMock()
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            await subscription_renewal_alert_job()


@pytest.mark.asyncio
async def test_sync_google_calendar_changes_tutor_loop_exception(seeded_session):
    """Test calendar sync catches exceptions thrown in the tutor iteration loop (lines 483-484)."""
    from app.core.scheduler import sync_google_calendar_changes_job
    tutor = Tutor(
        tg_id=44499,
        name="Tutor Calendar Test",
        is_active=True,
        google_token_json='{"token": "dummy"}'
    )
    seeded_session.add(tutor)
    await seeded_session.commit()

    with patch("app.services.google_calendar_service._execute_google_request", side_effect=Exception("HTTP Request Crash")):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            await sync_google_calendar_changes_job()
            # Loop exception caught, continues safely


@pytest.mark.asyncio
async def test_pending_bookings_reminder_bot_fails(seeded_session):
    """Test exception handling when bot fails in pending_bookings_reminder_job (lines 525-526)."""
    from app.core.scheduler import pending_bookings_reminder_job
    
    t = Tutor(tg_id=99988, name="T1", is_active=True)
    seeded_session.add(t)
    await seeded_session.flush()
    
    b = Booking(
        student_id=1,
        tutor_id=t.id,
        service_type="Math",
        appointment_time=datetime.now(timezone.utc) + timedelta(days=1),
        status=BookingStatus.PENDING
    )
    seeded_session.add(b)
    await seeded_session.commit()
    
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = Exception("TG Send Failed")
    
    with patch("app.core.scheduler._get_bot", return_value=mock_bot):
        with patch("app.core.scheduler.async_session_factory", return_value=AsyncMock(__aenter__=AsyncMock(return_value=seeded_session), __aexit__=AsyncMock())):
            await pending_bookings_reminder_job()
            # Fails gracefully and logs error





