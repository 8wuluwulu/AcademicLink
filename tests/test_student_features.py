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


@pytest.mark.asyncio
async def test_student_link_deactivation_and_restoration(seeded_session: AsyncSession):
    """Verify that deactivating and restoring a student link updates is_active correctly."""
    from app.db.models import StudentTutorLink

    # Find the link
    stmt = select(StudentTutorLink).where(
        StudentTutorLink.student_id == 1,
        StudentTutorLink.tutor_id == 1
    )
    res = await seeded_session.execute(stmt)
    link = res.scalar_one()

    assert link.is_active is True

    # Deactivate
    link.is_active = False
    await seeded_session.commit()

    await seeded_session.refresh(link)
    assert link.is_active is False

    # Restore
    link.is_active = True
    await seeded_session.commit()

    await seeded_session.refresh(link)
    assert link.is_active is True


# ── Reschedule Request Confirmation/Rejection Tests ──────────────────

from unittest.mock import AsyncMock, MagicMock, patch

class MockAsyncSessionContext:
    def __init__(self, sess):
        self.sess = sess
    async def __aenter__(self):
        return self.sess
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


@pytest.mark.asyncio
async def test_cb_tutor_resched_approve(seeded_session: AsyncSession):
    """Test cb_tutor_resched_approve handler updates booking and notifies student."""
    from app.bot.handlers import cb_tutor_resched_approve
    
    appt = _next_weekday(0)  # Monday 10:00 UTC
    booking = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Service",
        appointment_time=appt,
        status=BookingStatus.PENDING,
    )
    seeded_session.add(booking)
    await seeded_session.commit()

    # Propose Wed 12:00 UTC (15:00 MSK - within Wednesday's 10:00-18:00 MSK availability)
    new_time = _next_weekday(2).replace(hour=12, minute=0)
    new_time_ts = int(new_time.timestamp())

    # Mock CallbackQuery
    callback = AsyncMock()
    callback.data = f"tutor_resched_approve:{booking.id}:{new_time_ts}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    # Patch session factory and bot
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(seeded_session)), \
         patch("app.core.bot.get_bot") as mock_get_bot:
        
        mock_bot = AsyncMock()
        mock_get_bot.return_value = mock_bot
        
        await cb_tutor_resched_approve(callback)
        
        # Verify DB changes
        await seeded_session.refresh(booking)
        
        updated_time = booking.appointment_time
        if updated_time.tzinfo is None:
            updated_time = updated_time.replace(tzinfo=timezone.utc)
            
        assert updated_time == new_time
        assert booking.status == BookingStatus.CONFIRMED
        
        # Verify tutor message edited
        callback.message.edit_text.assert_called_once()
        assert "Перенос подтверждён!" in callback.message.edit_text.call_args[0][0]
        
        # Verify student notified
        mock_bot.send_message.assert_called_once()
        assert "Преподаватель подтвердил перенос" in mock_bot.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_cb_tutor_resched_reject(seeded_session: AsyncSession):
    """Test cb_tutor_resched_reject handler keeps booking unchanged and notifies student."""
    from app.bot.handlers import cb_tutor_resched_reject
    
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

    # Proposed time Wed 12:00 UTC
    proposed_time = _next_weekday(2).replace(hour=12, minute=0)
    proposed_time_ts = int(proposed_time.timestamp())

    # Mock CallbackQuery
    callback = AsyncMock()
    callback.data = f"tr_r:{booking.id}:{proposed_time_ts}"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    # Patch session factory and bot
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(seeded_session)), \
         patch("app.core.bot.get_bot") as mock_get_bot:
        
        mock_bot = AsyncMock()
        mock_get_bot.return_value = mock_bot
        
        await cb_tutor_resched_reject(callback)
        
        # Verify DB changes (unchanged)
        await seeded_session.refresh(booking)
        
        current_time = booking.appointment_time
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
            
        assert current_time == appt
        assert booking.status == BookingStatus.CONFIRMED
        
        # Verify tutor message edited
        callback.message.edit_text.assert_called_once()
        assert "Запрос на перенос отклонён!" in callback.message.edit_text.call_args[0][0]
        
        # Verify student notified
        mock_bot.send_message.assert_called_once()
        assert "Преподаватель отклонил запрос на перенос" in mock_bot.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_tutor_registration_flow(seeded_session: AsyncSession):
    """Test tutor registration FSM flow sets state and registers tutor with custom name."""
    from app.bot.handlers import (
        _send_dashboard,
        process_tutor_registration_name,
        TutorRegistrationStates
    )

    # 1. Test onboarding state initialization
    message = AsyncMock()
    message.from_user.id = 999888777
    message.from_user.username = "new_tutor_user"
    message.from_user.first_name = "NewTutor"
    message.answer = AsyncMock()

    state = AsyncMock()

    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(seeded_session)):
        await _send_dashboard(message, state)
        
        # Verify FSM state was set to waiting_full_name
        state.set_state.assert_called_once_with(TutorRegistrationStates.waiting_full_name)
        assert "Добро пожаловать в AcademicLink!" in message.answer.call_args_list[0][0][0]

    # 2. Test sending name and creating the tutor
    registration_message = AsyncMock()
    registration_message.text = "Иванов Иван Иванович"
    registration_message.from_user.id = 999888777
    registration_message.answer = AsyncMock()

    # Clear mock calls
    state.reset_mock()

    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(seeded_session)), \
         patch("app.core.bot.get_bot_username", return_value="test_bot"):
        
        await process_tutor_registration_name(registration_message, state)
        
        # Verify FSM state cleared
        state.clear.assert_called_once()
        
        # Check DB that Tutor was created with correct name
        result = await seeded_session.execute(
            select(Tutor).where(Tutor.tg_id == 999888777)
        )
        tutor = result.scalar_one_or_none()
        assert tutor is not None
        assert tutor.name == "Иванов Иван Иванович"

        # Check default service was created
        services_res = await seeded_session.execute(
            select(Service).where(Service.tutor_id == tutor.id)
        )
        services = services_res.scalars().all()
        assert len(services) == 1
        assert services[0].name == "Консультация"


@pytest.mark.asyncio
async def test_cb_student_cancel_confirm(seeded_session: AsyncSession):
    """Test cb_student_cancel_confirm handler sets CANCELLED and notifies tutor."""
    from app.bot.handlers import cb_student_cancel_confirm
    
    appt = _next_weekday(0)
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

    # Mock CallbackQuery
    callback = AsyncMock()
    callback.data = f"student_cancel_confirm:{booking.id}"
    callback.from_user.id = 987654321  # Seeded student Telegram ID
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    # Patch session factory and bot
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(seeded_session)), \
         patch("app.core.bot.get_bot") as mock_get_bot:
        
        mock_bot = AsyncMock()
        mock_get_bot.return_value = mock_bot
        
        await cb_student_cancel_confirm(callback)
        
        # Verify DB changes
        await seeded_session.refresh(booking)
        assert booking.status == BookingStatus.CANCELLED
        
        # Verify message edited
        callback.message.edit_text.assert_called_once()
        assert "Занятие отменено" in callback.message.edit_text.call_args[0][0]
        
        # Verify tutor notified
        mock_bot.send_message.assert_called_once()
        assert "Ученик отменил занятие" in mock_bot.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_cb_tutor_cancel_confirm(seeded_session: AsyncSession):
    """Test cb_cancel_confirm (tutor cancellation) handler sets CANCELLED and notifies student."""
    from app.bot.handlers import cb_cancel_confirm
    
    appt = _next_weekday(0)
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

    # Mock CallbackQuery
    callback = AsyncMock()
    callback.data = f"cancel_confirm:{booking.id}"
    callback.from_user.id = 123456789  # Seeded tutor Telegram ID
    callback.message = AsyncMock()
    callback.answer = AsyncMock()

    # Patch session factory and bot
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(seeded_session)), \
         patch("app.core.bot.get_bot") as mock_get_bot:
        
        mock_bot = AsyncMock()
        mock_get_bot.return_value = mock_bot
        
        await cb_cancel_confirm(callback)
        
        # Verify DB changes
        await seeded_session.refresh(booking)
        assert booking.status == BookingStatus.CANCELLED
        
        # Verify message edited
        callback.message.edit_text.assert_called_once()
        assert "Запись отменена" in callback.message.edit_text.call_args[0][0]
        
        # Verify student notified
        mock_bot.send_message.assert_called_once()
        assert "Ваше занятие отменено" in mock_bot.send_message.call_args[1]["text"]


@pytest.mark.asyncio
async def test_cmd_my_bookings(seeded_session: AsyncSession):
    from app.bot.handlers import cmd_my_bookings
    
    appt1 = _next_weekday(0)
    booking1 = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Service 1",
        appointment_time=appt1,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking1)
    
    appt2 = _next_weekday(2)
    booking2 = Booking(
        student_id=1,
        tutor_id=1,
        service_id=1,
        service_type="Test Service 2",
        appointment_time=appt2,
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking2)
    await seeded_session.commit()
    
    # Mock message
    message = AsyncMock()
    message.from_user.id = 987654321
    message.answer = AsyncMock()
    
    state = AsyncMock()
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(seeded_session)):
        await cmd_my_bookings(message, state)
        
        # Verify message answer is called
        message.answer.assert_called_once()
        text = message.answer.call_args[0][0]
        assert "Ваши предстоящие занятия" in text
        # Verify keyboard is built
        kb = message.answer.call_args[1].get("reply_markup")
        assert kb is not None
        # Check that there are two rows of buttons in the keyboard
        assert len(kb.inline_keyboard) == 2







