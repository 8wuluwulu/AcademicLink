"""
AcademicLink — Extra Unit Tests for Bot Handlers (app/bot/handlers.py)
"""

import json
import math
from datetime import datetime, time, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

MSK = timezone(timedelta(hours=3))

import pytest
import pytest_asyncio
from aiogram.types import ReplyKeyboardRemove, WebAppInfo, CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.db.models import Tutor, Student, StudentTutorLink, AvailabilitySlot, Booking, BookingStatus, Service, TutorAbsence
from app.bot.handlers import (
    cmd_start,
    cmd_home,
    cb_noop,
    cb_confirm,
    TutorCallbackMiddleware,
    TutorMessageMiddleware,
    _handle_non_tutor,
    _greeting,
    cmd_back,
    process_tutor_registration_name,
    start_student_registration,
    process_student_reg_name,
    process_student_reg_phone,
    cmd_book_select_tutor,
    cmd_my_bookings,
    cmd_schedule,
    cmd_new_requests,
    cb_page_sch,
    cb_page_new,
    cmd_students,
    cb_student_search_init,
    cb_student_history,
    process_student_phone,
    cmd_student_direct,
    cmd_settings,
    cb_manage_sbp,
    cb_sbp_set_phone,
    process_sbp_phone,
    cb_sbp_set_bank,
    process_sbp_bank,
    cmd_absence,
    cb_quick_block_today,
    cb_add_absence_init,
    process_absence_start,
    process_absence_end,
    cmd_del_absence,
    cb_del_absence,
    cmd_today,
    cb_tutor_edit_name,
    cb_student_edit_name,
    cb_student_edit_phone,
    process_tutor_edit_name,
    process_student_edit_name,
    process_student_edit_phone,
    cb_cancel_init,
    cb_cancel_confirm,
    cb_cancel_abort,
    cb_detail,
    cb_toggle,
    cb_gcal_disconnect,
    cb_gcal_localhost_warning,
    cb_student_delete_init,
    cb_student_delete_confirm,
    cb_student_delete_abort,
    cb_student_restore,
    cb_manage_slots,
    cb_back_to_settings,
    cb_slot_day,
    process_slot_times,
    cb_slot_clear_day,
    cb_slot_clear_all,
    cb_slot_clear_all_confirm,
    cb_toggle_remind,
    cb_manage_services,
    cb_add_service_init,
    process_service_name,
    process_service_duration,
    process_service_buffer,
    process_service_price,
    cmd_del_service,
    cb_del_service,
    cb_edit_service_select,
    cb_edit_srv_field,
    process_edit_service_name,
    process_edit_service_duration,
    process_edit_service_buffer,
    process_edit_service_price,
    cb_reschedule_init,
    process_reschedule_datetime,
    cb_tutor_resched_approve,
    cb_tutor_resched_reject,
    cb_manual_book_init,
    cb_student_cancel_init,
    cb_student_cancel_confirm,
    cb_student_cancel_abort,
    cb_student_reschedule_init,
    process_student_reschedule_datetime,
    cb_student_toggle_remind,
    process_manual_book_datetime,
    cb_manual_book_service,
    cmd_broadcast,
    process_broadcast_text,
    cb_broadcast_confirm,
    cb_broadcast_cancel,
    cb_confirm_p2p,
    cb_cancel_p2p,
    cmd_extend_sub,
    StudentSearch,
    StudentManagement,
    TutorAbsenceStates,
    SlotManagement,
    BroadcastStates,
    RescheduleStates,
    StudentRescheduleStates,
    ManualBookingStates,
    TutorSettingsStates,
    StudentSettingsStates,
    ServiceManagement,
    StudentRegistrationStates,
    TutorRegistrationStates,
)

# ── Custom Mock Subclasses to Bypass Pydantic Init Validation ─────────

class MockMessage(Message):
    def __init__(self, **kwargs):
        object.__setattr__(self, "text", "")
        object.__setattr__(self, "answer", AsyncMock())
        object.__setattr__(self, "reply_text", AsyncMock())
        object.__setattr__(self, "edit_text", AsyncMock())
        object.__setattr__(self, "edit_reply_markup", AsyncMock())
        object.__setattr__(self, "delete", AsyncMock())
        object.__setattr__(self, "from_user", MagicMock(id=111, username="tutor_1"))
        for k, v in kwargs.items():
            object.__setattr__(self, k, v)
            
    def __getattr__(self, name):
        val = AsyncMock() if name in ("answer", "edit_text", "edit_reply_markup", "reply_text", "delete") else MagicMock()
        object.__setattr__(self, name, val)
        return val

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)


class MockCallbackQuery(CallbackQuery):
    def __init__(self, **kwargs):
        object.__setattr__(self, "id", "123")
        object.__setattr__(self, "data", "noop")
        object.__setattr__(self, "answer", AsyncMock())
        object.__setattr__(self, "message", MockMessage())
        object.__setattr__(self, "from_user", MagicMock(id=111, username="tutor_1"))
        for k, v in kwargs.items():
            object.__setattr__(self, k, v)
            
    def __getattr__(self, name):
        val = AsyncMock() if name in ("answer", "edit_text", "edit_reply_markup", "reply_text", "delete") else MagicMock()
        object.__setattr__(self, name, val)
        return val

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)



# ── Fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def test_engine():
    """In-memory SQLite for bot handler tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def test_session_factory(test_engine):
    """Session factory bound to the test engine."""
    return async_session_factory


@pytest_asyncio.fixture
async def db_session(test_engine):
    """Seed data: tutor, student, link and return session."""
    session_factory = async_session_maker = async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )
    async with session_factory() as session:
        # 1. Existing active tutor with active subscription expiring in 2 days
        t_active = Tutor(
            id=1,
            tg_id=111,
            name="Active Tutor",
            is_active=True,
            subscription_status="active",
            subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        )
        # 2. Existing paused tutor with expired subscription
        t_paused = Tutor(
            id=2,
            tg_id=222,
            name="Paused Tutor",
            is_active=False,
            subscription_status="expired",
            subscription_expires_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
        # 3. Tutor with no subscription record
        t_no_sub = Tutor(
            id=3,
            tg_id=333,
            name="No Sub Tutor",
            is_active=True,
            subscription_status=None,
            subscription_expires_at=None,
        )
        session.add_all([t_active, t_paused, t_no_sub])
        await session.flush()

        # 4. Student linked to tutor 1
        s_linked = Student(
            id=10,
            full_name="Linked Student",
            phone="+79001112233",
            telegram_id=1000,
            wants_reminders=True,
            telegram_username="student_linked",
        )
        # 5. Student unlinked (deleted from database or no active links)
        s_unlinked = Student(
            id=20,
            full_name="Unlinked Student",
            phone="+79002223344",
            telegram_id=2000,
        )
        # 6. Unlinked student by username (for auto-linking test)
        s_username = Student(
            id=30,
            full_name="Username Student",
            phone="+79003334455",
            telegram_id=None,
            telegram_username="student_user",
        )
        session.add_all([s_linked, s_unlinked, s_username])
        await session.flush()

        link = StudentTutorLink(
            student_id=10,
            tutor_id=1,
            is_active=True,
        )
        link2 = StudentTutorLink(
            student_id=20,
            tutor_id=1,
            is_active=False,
        )
        session.add_all([link, link2])
        await session.commit()

    async with session_factory() as session:
        yield session


# ── Context Helper ───────────────────────────────────────────────────

class MockAsyncSessionContext:
    def __init__(self, sess):
        self.sess = sess
    async def __aenter__(self):
        return self.sess
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


# ── Tests ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_start_new_user_starts_onboarding(db_session):
    """If the user is completely new, cmd_start should start the Tutor Registration flow."""
    message = MockMessage(
        from_user=MagicMock(id=99999, username="new_user", first_name="New"),
        text="/start",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        
        state.set_state.assert_called_once()
        message.answer.assert_called_once()
        assert "Иванов Иван Иванович" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_start_referral_links(db_session):
    """If /start has a referral code, it should trigger student registration."""
    message = MockMessage(
        from_user=MagicMock(id=99999, username="new_student", first_name="Student"),
        text="/start ref_1",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        assert message.answer.call_count >= 1
        assert "Фамилию" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_start_gcal_success_banner(db_session):
    """If /start has gcal_success parameter, it shows connection banner."""
    message = MockMessage(
        from_user=MagicMock(id=111, username="tutor_1", first_name="Tutor"),
        text="/start gcal_success",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        any_gcal_banner = any("Google Календарь успешно подключен!" in call[0][0] for call in message.answer.call_args_list)
        assert any_gcal_banner is True


@pytest.mark.asyncio
async def test_cmd_start_tutor_dashboard_expires_soon(db_session):
    """Existing tutor dashboard displays subscription warning when expiring soon."""
    message = MockMessage(
        from_user=MagicMock(id=111, username="tutor_1", first_name="Active Tutor"),
        text="/start",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        dashboard_msg = message.answer.call_args[0][0]
        assert "Active Tutor" in dashboard_msg
        assert "Активен" in dashboard_msg
        assert "истекает через" in dashboard_msg


@pytest.mark.asyncio
async def test_cmd_start_tutor_dashboard_expired(db_session):
    """Existing paused tutor dashboard displays expired subscription banner."""
    message = MockMessage(
        from_user=MagicMock(id=222, username="tutor_2", first_name="Paused Tutor"),
        text="/start",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        dashboard_msg = message.answer.call_args[0][0]
        assert "Пауза" in dashboard_msg
        assert "Подписка истекла!" in dashboard_msg


@pytest.mark.asyncio
async def test_cmd_start_tutor_dashboard_no_sub(db_session):
    """Tutor dashboard displays warning banner when subscription is not configured."""
    message = MockMessage(
        from_user=MagicMock(id=333, username="tutor_3", first_name="No Sub Tutor"),
        text="/start",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        dashboard_msg = message.answer.call_args[0][0]
        assert "Подписка не оформлена!" in dashboard_msg


@pytest.mark.asyncio
async def test_cmd_start_linked_student(db_session):
    """Active linked student start displays student menu."""
    message = MockMessage(
        from_user=MagicMock(id=1000, username="student_linked", first_name="Linked"),
        text="/start",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        assert message.answer.call_count == 2
        greeting_msg = message.answer.call_args_list[0][0][0]
        assert "Linked Student" in greeting_msg
        assert "Вы вошли как ученик" in greeting_msg


@pytest.mark.asyncio
async def test_cmd_start_unlinked_student(db_session):
    """Deleted student start shows limited access message."""
    message = MockMessage(
        from_user=MagicMock(id=2000, username="student_unlinked", first_name="Unlinked"),
        text="/start",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        msg = message.answer.call_args[0][0]
        assert "Доступ ограничен" in msg
        assert "Вы были удалены из базы" in msg


@pytest.mark.asyncio
async def test_cmd_start_student_auto_linking(db_session):
    """Student matches by username to an unlinked record and links telegram_id automatically."""
    message = MockMessage(
        from_user=MagicMock(id=3000, username="student_user", first_name="AutoLink"),
        text="/start",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        msg = message.answer.call_args[0][0]
        assert "Username Student" in msg
        assert "Ваш профиль успешно привязан" in msg
        
        student = await db_session.get(Student, 30)
        assert student.telegram_id == 3000


@pytest.mark.asyncio
async def test_cmd_home_redirects_to_dashboard(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        text="🏠 Главная",
        answer=AsyncMock(),
    )
    state = AsyncMock(spec=FSMContext)
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_home(message, state)
        assert message.answer.call_count == 1
        assert "Active Tutor" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cb_noop():
    callback = MockCallbackQuery(
        answer=AsyncMock(),
    )
    await cb_noop(callback)
    callback.answer.assert_called_once()


@pytest.mark.asyncio
async def test_cb_confirm(db_session):
    callback = MockCallbackQuery(
        data="confirm:10",
        answer=AsyncMock(),
        message=MockMessage(
            edit_text=AsyncMock(),
        ),
    )
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.services.google_calendar_service.sync_booking_to_calendar", AsyncMock()):
        await cb_confirm(callback)
        callback.answer.assert_called_once()


# ── Middlewares & Helper Tests ────────────────────────────────────────

@pytest.mark.asyncio
async def test_tutor_callback_middleware_student(db_session):
    middleware = TutorCallbackMiddleware()
    handler = AsyncMock(return_value="OK")
    event = MockCallbackQuery(
        data="student_cancel:123",
    )
    data = {}
    res = await middleware(handler, event, data)
    assert res == "OK"


@pytest.mark.asyncio
async def test_tutor_callback_middleware_admin_sub(db_session):
    middleware = TutorCallbackMiddleware()
    handler = AsyncMock(return_value="ADMIN_OK")
    event = MockCallbackQuery(
        data="admin_sub_give_manual",
    )
    data = {}
    res = await middleware(handler, event, data)
    assert res == "ADMIN_OK"



@pytest.mark.asyncio
async def test_tutor_callback_middleware_non_tutor(db_session):
    middleware = TutorCallbackMiddleware()
    handler = AsyncMock()
    event = MockCallbackQuery(
        data="confirm:10",
        from_user=MagicMock(id=9999),
        answer=AsyncMock(),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await middleware(handler, event, {})
        event.answer.assert_called_with("⚠️ Это действие доступно только репетиторам.", show_alert=True)


@pytest.mark.asyncio
async def test_tutor_callback_middleware_expired_sub(db_session):
    middleware = TutorCallbackMiddleware()
    handler = AsyncMock()
    event = MockCallbackQuery(
        data="confirm:10",
        from_user=MagicMock(id=222),
        answer=AsyncMock(),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await middleware(handler, event, {})
        assert event.answer.call_count == 1
        assert "Ваша подписка" in event.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_tutor_message_middleware_admin(db_session):
    middleware = TutorMessageMiddleware()
    handler = AsyncMock(return_value="OK")
    event = MockMessage(
        from_user=MagicMock(id=55555),
        text="/other_cmd",
    )
    data = {}
    from app.core.config import settings
    with patch.object(settings, "admin_tg_id", 55555):
        res = await middleware(handler, event, data)
        assert res == "OK"


@pytest.mark.asyncio
async def test_tutor_message_middleware_start(db_session):
    middleware = TutorMessageMiddleware()
    handler = AsyncMock(return_value="OK")
    event = MockMessage(
        from_user=MagicMock(id=111),
        text="/start",
    )
    data = {}
    res = await middleware(handler, event, data)
    assert res == "OK"


@pytest.mark.asyncio
async def test_tutor_message_middleware_expired_tutor(db_session):
    middleware = TutorMessageMiddleware()
    handler = AsyncMock()
    event = MockMessage(
        from_user=MagicMock(id=222),
        text="📅 Расписание",
        answer=AsyncMock(),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        from app.bot.handlers import _tutor_cache
        _tutor_cache.clear()
        await middleware(handler, event, {})
        assert event.answer.call_count == 1
        assert "подписка" in event.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_non_tutor_student(db_session):
    message = MockMessage(
        from_user=MagicMock(id=1000, username="student_linked"),
        answer=AsyncMock(),
    )
    await _handle_non_tutor(message, db_session)
    assert message.answer.call_count == 1
    assert "Вы зарегистрированы как ученик" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_non_tutor_deactivated(db_session):
    message = MockMessage(
        from_user=MagicMock(id=2000),
        answer=AsyncMock(),
    )
    await _handle_non_tutor(message, db_session)
    assert message.answer.call_count == 1
    assert "Доступ ограничен" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_handle_non_tutor_new_user(db_session):
    message = MockMessage(
        from_user=MagicMock(id=9999),
        answer=AsyncMock(),
    )
    await _handle_non_tutor(message, db_session)
    assert message.answer.call_count == 1
    assert "Добро пожаловать в AcademicLink" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_greeting():
    with patch("app.bot.handlers.datetime") as mock_datetime:
        mock_datetime.now.return_value.hour = 5
        assert _greeting() == "Доброй ночи"
        mock_datetime.now.return_value.hour = 10
        assert _greeting() == "Доброе утро"
        mock_datetime.now.return_value.hour = 15
        assert _greeting() == "Добрый день"
        mock_datetime.now.return_value.hour = 20
        assert _greeting() == "Добрый вечер"


@pytest.mark.asyncio
async def test_cmd_back(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    state.clear = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_back(message, state)
        state.clear.assert_called_once()
        assert message.answer.call_count == 1


# ── Tutor & Student Registration ─────────────────────────────────────

@pytest.mark.asyncio
async def test_process_tutor_registration_name_invalid():
    message = MockMessage(
        text="Ivanov",
        answer=AsyncMock(),
    )
    state = AsyncMock()
    await process_tutor_registration_name(message, state)
    assert "Пожалуйста, введите ваши корректные" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_process_tutor_registration_name_valid(db_session):
    message = MockMessage(
        text="Иванов Иван Иванович",
        from_user=MagicMock(id=444),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.core.bot.get_bot_username", return_value="my_test_bot"):
        await process_tutor_registration_name(message, state)
        assert message.answer.call_count == 2
        result = await db_session.execute(select(Tutor).where(Tutor.tg_id == 444))
        tutor = result.scalar_one_or_none()
        assert tutor is not None
        assert tutor.name == "Иванов Иван Иванович"


@pytest.mark.asyncio
async def test_start_student_registration_not_found(db_session):
    message = MockMessage(
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await start_student_registration(message, state, 999)
        assert "Преподаватель не найден" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_start_student_registration_self(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await start_student_registration(message, state, 1)
        assert "собственной ссылке" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_start_student_registration_already_linked(db_session):
    message = MockMessage(
        from_user=MagicMock(id=1000),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await start_student_registration(message, state, 1)
        assert message.answer.call_count == 2


@pytest.mark.asyncio
async def test_start_student_registration_archived(db_session):
    message = MockMessage(
        from_user=MagicMock(id=2000),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await start_student_registration(message, state, 1)
        assert "Доступ ограничен" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_start_student_registration_other_tutor(db_session):
    message = MockMessage(
        from_user=MagicMock(id=1000, username="student_linked"),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await start_student_registration(message, state, 3)
        assert "Вы успешно прикрепились" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_process_student_reg_name():
    message = MockMessage(
        text="A",
        answer=AsyncMock(),
    )
    state = AsyncMock()
    await process_student_reg_name(message, state)
    assert "Имя и Фамилию" in message.answer.call_args[0][0]

    message.text = "Иван Иванов"
    await process_student_reg_name(message, state)
    state.update_data.assert_called_with(reg_full_name="Иван Иванов")
    state.set_state.assert_called_once()


@pytest.mark.asyncio
async def test_process_student_reg_phone_contact(db_session):
    message = MockMessage(
        contact=MagicMock(phone_number="79008888888"),
        from_user=MagicMock(id=7777, username="new_std_uname"),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    state.get_data.return_value = {"reg_full_name": "New Student", "reg_tutor_id": 1}
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await process_student_reg_phone(message, state)
        assert "Регистрация успешно завершена" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_process_student_reg_phone_text_normalized(db_session):
    message = MockMessage(
        contact=None,
        text="89009999999",
        from_user=MagicMock(id=8888, username="new_std_uname2"),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    state.get_data.return_value = {"reg_full_name": "New Student 2", "reg_tutor_id": 1}
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await process_student_reg_phone(message, state)
        assert "+79009999999" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_process_student_reg_phone_invalid():
    message = MockMessage(
        contact=None,
        text="invalid",
        answer=AsyncMock(),
    )
    state = AsyncMock()
    await process_student_reg_phone(message, state)
    assert "Номер телефона" in message.answer.call_args[0][0]


# ── Student Booking List & Book ──────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_book_select_tutor_unregistered(db_session):
    message = MockMessage(
        from_user=MagicMock(id=9999),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_book_select_tutor(message, state)
        assert "Вы не зарегистрированы" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_book_select_tutor_registered(db_session):
    message = MockMessage(
        from_user=MagicMock(id=1000, username="student_linked"),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_book_select_tutor(message, state)
        assert message.answer.call_count == 2


@pytest.mark.asyncio
async def test_cmd_my_bookings_none(db_session):
    message = MockMessage(
        from_user=MagicMock(id=1000),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_my_bookings(message, state)
        assert "нет предстоящих записей" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_my_bookings_existing(db_session):
    b = Booking(
        id=44,
        tutor_id=1,
        student_id=10,
        service_type="Консультация",
        appointment_time=datetime.now(timezone.utc) + timedelta(hours=2),
        price=1500,
        status=BookingStatus.CONFIRMED,
    )
    db_session.add(b)
    await db_session.commit()
    message = MockMessage(
        from_user=MagicMock(id=1000),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_my_bookings(message, state)
        assert "Ваши предстоящие занятия" in message.answer.call_args[0][0]


# ── Schedule & Paginated Views ───────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_schedule_non_tutor(db_session):
    message = MockMessage(
        from_user=MagicMock(id=1000),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_schedule(message, state)
        assert "Этот раздел доступен только репетиторам" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_schedule_tutor(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_schedule(message, state)
        assert "Расписание" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_new_requests_tutor(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_new_requests(message, state)
        assert "Новые заявки" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cb_page_sch(db_session):
    callback = MockCallbackQuery(
        data="page_sch:0",
        from_user=MagicMock(id=111),
        message=MockMessage(
            edit_text=AsyncMock(),
        ),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_page_sch(callback)
        callback.message.edit_text.assert_called_once()


@pytest.mark.asyncio
async def test_cb_page_new(db_session):
    callback = MockCallbackQuery(
        data="page_new:0",
        from_user=MagicMock(id=111),
        message=MockMessage(
            edit_text=AsyncMock(),
        ),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_page_new(callback)
        callback.message.edit_text.assert_called_once()


# ── Student Management (History, Search, Delete, Restore) ────────────

@pytest.mark.asyncio
async def test_cmd_students_non_tutor(db_session):
    message = MockMessage(
        from_user=MagicMock(id=1000),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_students(message, state)
        assert "Этот раздел доступен только репетиторам" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_students_tutor_no_students(db_session):
    message = MockMessage(
        from_user=MagicMock(id=333),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_students(message, state)
        assert "У вас пока нет активных учеников" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_students_tutor_with_students(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_students(message, state)
        assert "Linked Student" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cb_student_search_init():
    callback = MockCallbackQuery(
        message=MockMessage(
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    await cb_student_search_init(callback, state)
    state.set_state.assert_called_once()
    assert "Поиск ученика" in callback.message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cb_student_history(db_session):
    callback = MockCallbackQuery(
        data="student_history:10",
        from_user=MagicMock(id=111),
        message=MockMessage(
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_student_history(callback)
        assert "Linked Student" in callback.message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_process_student_phone(db_session):
    message = MockMessage(
        text="invalid_phone",
        answer=AsyncMock(),
    )
    state = AsyncMock()
    await process_student_phone(message, state)
    assert "Введите корректный номер" in message.answer.call_args[0][0]

    message.text = "+79001112233"
    message.from_user = MagicMock(id=111)
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await process_student_phone(message, state)
        assert "Linked Student" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_cmd_student_direct(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        text="/student",
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_student_direct(message, state)
        assert "Использование" in message.answer.call_args[0][0]

        message.text = "/student +79001112233"
        await cmd_student_direct(message, state)
        assert "Linked Student" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_student_deletion_flow(db_session):
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        data="student_delete_init:10",
        message=MockMessage(
            answer=AsyncMock(),
        ),
    )
    state = AsyncMock()
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_student_delete_init(callback, state)
        assert "Удаление ученика" in callback.message.answer.call_args[0][0]
        
        callback.data = "student_delete_confirm:10"
        callback.message.edit_text = AsyncMock()
        state.get_data.return_value = {"delete_student_id": 10}
        await cb_student_delete_confirm(callback, state)
        assert "успешно удален" in callback.message.edit_text.call_args[0][0]
        
        # Verify link is inactive
        link = await db_session.get(StudentTutorLink, (10, 1))
        assert link.is_active is False
        
        # cb_student_restore
        callback.data = "student_restore:10"
        callback.message.edit_text.reset_mock()
        await cb_student_restore(callback)
        assert "успешно восстановлен" in callback.message.edit_text.call_args[0][0]
        
        # Verify link is active again
        db_session.expunge_all()
        link = await db_session.get(StudentTutorLink, (10, 1))
        assert link.is_active is True
        
        # cb_student_delete_abort
        callback.message.edit_text.reset_mock()
        await cb_student_delete_abort(callback, state)
        assert "Удаление отменено" in callback.message.edit_text.call_args[0][0]


# ── Settings & SBP Bank Configurations ───────────────────────────────

@pytest.mark.asyncio
async def test_settings_commands(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_settings(message, state)
        assert "Настройки" in message.answer.call_args[0][0]
        
        callback = MockCallbackQuery(
            from_user=MagicMock(id=111),
            message=MockMessage(
                edit_text=AsyncMock(),
            ),
        )
        await cb_manage_sbp(callback, state)
        assert "Реквизиты СБП" in callback.message.edit_text.call_args[0][0]
        
        callback.message.edit_text = AsyncMock()
        await cb_sbp_set_phone(callback, state)
        assert "Введите ваш номер" in callback.message.edit_text.call_args[0][0]
        
        message.text = "invalid"
        await process_sbp_phone(message, state)
        assert "Неверный формат" in message.answer.call_args[0][0]
        
        message.text = "+79001112233"
        await process_sbp_phone(message, state)
        assert any("успешно сохранен" in call[0][0] for call in message.answer.call_args_list)
        
        callback.message.edit_text = AsyncMock()
        await cb_sbp_set_bank(callback, state)
        assert "Введите название" in callback.message.edit_text.call_args[0][0]
        
        message.text = "Сбербанк"
        await process_sbp_bank(message, state)
        assert any("успешно сохранен" in call[0][0] for call in message.answer.call_args_list)


# ── Profile Edits (Name & Phone) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_profile_edits(db_session):
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        message=MockMessage(
            answer=AsyncMock(),
        ),
    )
    state = AsyncMock()
    
    await cb_tutor_edit_name(callback, state)
    state.set_state.assert_called_with(TutorSettingsStates.waiting_name)
    
    await cb_student_edit_name(callback, state)
    state.set_state.assert_called_with(StudentSettingsStates.waiting_name)
    
    await cb_student_edit_phone(callback, state)
    state.set_state.assert_called_with(StudentSettingsStates.waiting_phone)
    
    # process_tutor_edit_name
    message = MockMessage(
        from_user=MagicMock(id=111),
        text="/cancel",
        answer=AsyncMock(),
    )
    state.clear = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await process_tutor_edit_name(message, state)
        state.clear.assert_called_once()
        
        message.text = "Invalid"
        await process_tutor_edit_name(message, state)
        assert "ФИО должно содержать" in message.answer.call_args[0][0]
        
        message.text = "Новый Репетитор"
        await process_tutor_edit_name(message, state)
        assert any("успешно обновлено" in call[0][0] for call in message.answer.call_args_list)
        
        # process_student_edit_name
        message.text = "/cancel"
        state.clear.reset_mock()
        await process_student_edit_name(message, state)
        state.clear.assert_called()
        
        message.text = "New StudentName"
        message.from_user = MagicMock(id=1000)
        await process_student_edit_name(message, state)
        assert any("успешно обновлено" in call[0][0] for call in message.answer.call_args_list)
        
        # process_student_edit_phone
        message.text = "/cancel"
        state.clear.reset_mock()
        await process_student_edit_phone(message, state)
        state.clear.assert_called()
        
        message.text = "invalid"
        await process_student_edit_phone(message, state)
        assert "Номер телефона" in message.answer.call_args[0][0]
        
        message.text = "+79001112233"
        await process_student_edit_phone(message, state)
        assert any("успешно обновлен" in call[0][0] for call in message.answer.call_args_list)


# ── Google Calendar Integrations ─────────────────────────────────────

@pytest.mark.asyncio
async def test_gcal_disconnect_and_warning(db_session):
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        message=MockMessage(
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_gcal_disconnect(callback)
        callback.answer.assert_called_with("📅 Google Календарь отключен.")
        
        callback.message.edit_text.reset_mock()
        await cb_gcal_localhost_warning(callback)
        assert "Режим разработки" in callback.answer.call_args[0][0]
        assert callback.answer.call_args[1].get("show_alert") is True


# ── Availability Slot Configurations ─────────────────────────────────

@pytest.mark.asyncio
async def test_slot_management(db_session):
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        message=MockMessage(
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_manage_slots(callback, state)
        assert callback.message.edit_text.call_count == 1
        
        callback.message.edit_text.reset_mock()
        await cb_back_to_settings(callback, state)
        assert callback.message.edit_text.call_count == 1
        
        callback.message.edit_text.reset_mock()
        callback.data = "slot_day:0"
        await cb_slot_day(callback, state)
        state.set_state.assert_called_with(SlotManagement.entering_times)
        
        # process_slot_times
        message = MockMessage(
            from_user=MagicMock(id=111),
            text="◀️ Назад",
            answer=AsyncMock(),
        )
        state.clear = AsyncMock()
        await process_slot_times(message, state)
        state.clear.assert_called_once()
        
        message.text = "invalid"
        await process_slot_times(message, state)
        assert "Неверный формат" in message.answer.call_args[0][0]
        
        message.text = "25:00-28:00"
        await process_slot_times(message, state)
        assert "Некорректное время" in message.answer.call_args[0][0]
        
        message.text = "18:00-09:00"
        await process_slot_times(message, state)
        assert "должно быть раньше" in message.answer.call_args[0][0]
        
        message.text = "09:00-18:00"
        state.get_data.return_value = {"slot_weekday": 0, "tutor_id": 1}
        await process_slot_times(message, state)
        assert "успешно обновлён" in message.answer.call_args[0][0]
        
        # cb_slot_clear_day
        callback.data = "slot_clear_day:0"
        callback.message.edit_text.reset_mock()
        await cb_slot_clear_day(callback, state)
        assert callback.message.edit_text.call_count == 1
        
        # cb_slot_clear_all
        await cb_slot_clear_all(callback, state)
        assert "Очистить все слоты" in callback.message.edit_text.call_args[0][0]
        
        # cb_slot_clear_all_confirm
        await cb_slot_clear_all_confirm(callback)
        assert callback.message.edit_text.call_count >= 1


@pytest.mark.asyncio
async def test_toggle_remind(db_session):
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        data="toggle_remind:1",
        message=MockMessage(
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_toggle_remind(callback)
        assert callback.message.edit_text.call_count == 1


# ── Services Management (CRUD) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_services_management(db_session):
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        message=MockMessage(
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_manage_services(callback, state)
        assert callback.message.edit_text.call_count == 1
        
        callback.message.answer = AsyncMock()
        await cb_add_service_init(callback, state)
        state.set_state.assert_called_with(ServiceManagement.waiting_name)
        
        # FSM flows
        message = MockMessage(
            from_user=MagicMock(id=111),
            text="Физика",
            answer=AsyncMock(),
        )
        await process_service_name(message, state)
        state.set_state.assert_called_with(ServiceManagement.waiting_duration)
        
        message.text = "invalid"
        await process_service_duration(message, state)
        assert "Введите число" in message.answer.call_args[0][0]
        
        message.text = "60"
        await process_service_duration(message, state)
        state.set_state.assert_called_with(ServiceManagement.waiting_buffer)
        
        message.text = "invalid"
        await process_service_buffer(message, state)
        assert "Введите число" in message.answer.call_args[0][0]
        
        message.text = "15"
        await process_service_buffer(message, state)
        state.set_state.assert_called_with(ServiceManagement.waiting_price)
        
        message.text = "invalid"
        await process_service_price(message, state)
        assert "Введите число" in message.answer.call_args[0][0]
        
        message.text = "1800"
        state.get_data.return_value = {"srv_name": "Физика", "srv_duration": 60, "srv_buffer": 15}
        await process_service_price(message, state)
        assert any("успешно добавлена" in call[0][0] for call in message.answer.call_args_list)
        
        # Verify service in DB
        result = await db_session.execute(select(Service).where(Service.name == "Физика"))
        srv = result.scalar_one_or_none()
        assert srv is not None
        
        # cmd_del_service
        message.text = f"/del_service_{srv.id}"
        message.answer.reset_mock()
        await cmd_del_service(message)
        assert any("удалена" in call[0][0] for call in message.answer.call_args_list)


@pytest.mark.asyncio
async def test_cb_del_service(db_session):
    srv = Service(tutor_id=1, name="Химия", duration=60, buffer_time=15, price=1000)
    db_session.add(srv)
    await db_session.commit()
    
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        data=f"del_service_cb:{srv.id}",
        message=MockMessage(
            edit_text=AsyncMock(),
        ),
        answer=AsyncMock(),
    )
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_del_service(callback)
        assert callback.message.edit_text.call_count == 1
        
        # Reactivate service for edit test
        srv.is_active = True
        await db_session.commit()
        
        # cb_edit_service_select
        callback.data = f"edit_service_select:{srv.id}"
        callback.message.edit_text.reset_mock()
        await cb_edit_service_select(callback, AsyncMock())
        assert callback.message.edit_text.call_count == 1
        
        # cb_edit_srv_field
        state = AsyncMock()
        callback.data = f"edit_srv_field:name:{srv.id}"
        callback.message.answer = AsyncMock()
        await cb_edit_srv_field(callback, state)
        state.set_state.assert_called_with(ServiceManagement.editing_name)
        
        callback.data = f"edit_srv_field:duration:{srv.id}"
        await cb_edit_srv_field(callback, state)
        state.set_state.assert_called_with(ServiceManagement.editing_duration)
        
        callback.data = f"edit_srv_field:buffer:{srv.id}"
        await cb_edit_srv_field(callback, state)
        state.set_state.assert_called_with(ServiceManagement.editing_buffer)
        
        callback.data = f"edit_srv_field:price:{srv.id}"
        await cb_edit_srv_field(callback, state)
        state.set_state.assert_called_with(ServiceManagement.editing_price)


@pytest.mark.asyncio
async def test_process_edit_srv_fields(db_session):
    srv = Service(tutor_id=1, name="Биология", duration=60, buffer_time=15, price=1000)
    db_session.add(srv)
    await db_session.commit()
    
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    state.get_data.return_value = {"editing_service_id": srv.id}
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        message.text = "Новая Биология"
        await process_edit_service_name(message, state)
        assert any("успешно изменено" in call[0][0] for call in message.answer.call_args_list)
        
        message.text = "90"
        await process_edit_service_duration(message, state)
        assert any("успешно изменена" in call[0][0] for call in message.answer.call_args_list)
        
        message.text = "30"
        await process_edit_service_buffer(message, state)
        assert any("успешно изменен" in call[0][0] for call in message.answer.call_args_list)
        
        message.text = "2200"
        await process_edit_service_price(message, state)
        assert any("успешно изменена" in call[0][0] for call in message.answer.call_args_list)


# ── Absences (Quick Block & Date Ranges) ──────────────────────────────

@pytest.mark.asyncio
async def test_absence_commands(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_absence(message, state)
        assert "Моё отсутствие" in message.answer.call_args[0][0]
        
        callback = MockCallbackQuery(
            from_user=MagicMock(id=111),
            message=MockMessage(
                edit_text=AsyncMock(),
            ),
        )
        await cb_quick_block_today(callback)
        assert callback.message.answer.call_count == 2
        
        await cb_add_absence_init(callback, state)
        state.set_state.assert_called_once()
        
        message.text = "invalid"
        await process_absence_start(message, state)
        assert "Неверный формат" in message.answer.call_args[0][0]
        
        message.text = "25.12.2026 10:00"
        await process_absence_start(message, state)
        state.update_data.assert_called_with(start_time=datetime(2026, 12, 25, 10, 0, tzinfo=MSK).astimezone(timezone.utc).isoformat())
        
        message.text = "invalid"
        await process_absence_end(message, state)
        assert "Неверный формат" in message.answer.call_args[0][0]
        
        message.text = "26.12.2026 18:00"
        state.get_data.return_value = {"start_time": datetime(2026, 12, 25, 10, 0, tzinfo=MSK).astimezone(timezone.utc).isoformat()}
        await process_absence_end(message, state)
        assert any("отсутствия добавлен" in call[0][0] for call in message.answer.call_args_list)
        
        message.text = "/del_absence_1"
        await cmd_del_absence(message)
        assert any("отсутствия удален" in call[0][0] for call in message.answer.call_args_list)
        
        a = TutorAbsence(
            tutor_id=1,
            start_time=datetime(2026,12,25, tzinfo=timezone.utc),
            end_time=datetime(2026,12,26, tzinfo=timezone.utc),
            reason="Vacation"
        )
        db_session.add(a)
        await db_session.commit()
        callback.data = f"del_absence:{a.id}"
        await cb_del_absence(callback)
        assert callback.message.edit_text.call_count == 1


# ── Schedule Briefings ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_today(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_today(message, state)
        assert "занятий" in message.answer.call_args[0][0]


# ── Bookings Operations (Detail, Cancel, Reschedule, Manual Book) ─────

@pytest.mark.asyncio
async def test_booking_actions(db_session):
    b = Booking(
        id=66,
        tutor_id=1,
        student_id=10,
        service_type="Консультация",
        appointment_time=datetime.now(timezone.utc) + timedelta(hours=2),
        price=1500,
        status=BookingStatus.PENDING,
    )
    db_session.add(b)
    await db_session.commit()
    
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        data="reschedule_init:66",
        message=MockMessage(
            answer=AsyncMock(),
        ),
    )
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_reschedule_init(callback, AsyncMock())
        assert callback.message.answer.call_count == 1
        
        # process_reschedule_datetime
        message = MockMessage(
            from_user=MagicMock(id=111),
            text="invalid",
            answer=AsyncMock(),
        )
        state = AsyncMock()
        state.get_data.return_value = {"reschedule_booking_id": 66}
        await process_reschedule_datetime(message, state)
        assert "Неверный формат" in message.answer.call_args[0][0]
        
        # cb_tutor_resched_approve
        callback.data = f"tr_a:66:{int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())}"
        callback.message.edit_text = AsyncMock()
        with patch("app.services.google_calendar_service.sync_booking_to_calendar", AsyncMock()):
            # Mock validation services
            with patch("app.services.booking_service.check_availability", AsyncMock()), \
                 patch("app.services.booking_service.check_tutor_absence", AsyncMock()), \
                 patch("app.services.booking_service.check_double_booking", AsyncMock()):
                await cb_tutor_resched_approve(callback)
                assert callback.message.edit_text.call_count == 1
            
        # cb_tutor_resched_reject
        callback.data = f"tr_r:66:{int((datetime.now(timezone.utc) + timedelta(days=2)).timestamp())}"
        callback.message.edit_text.reset_mock()
        await cb_tutor_resched_reject(callback)
        assert callback.message.edit_text.call_count == 1


@pytest.mark.asyncio
async def test_manual_booking_flow(db_session):
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        data="manual_book_init:10",
        message=MockMessage(
            answer=AsyncMock(),
        ),
    )
    state = AsyncMock()
    
    # We need an active service in DB for manual book FSM to transition
    srv = Service(tutor_id=1, name="Тест", duration=60, buffer_time=15, price=1000)
    db_session.add(srv)
    await db_session.commit()
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_manual_book_init(callback, state)
        assert callback.message.answer.call_count == 1
        
        # process_manual_book_datetime
        message = MockMessage(
            from_user=MagicMock(id=111),
            text="25.12.2026 14:00",
            answer=AsyncMock(),
        )
        state.get_data.return_value = {"manual_book_student_id": 10, "manual_book_tutor_id": 1}
        await process_manual_book_datetime(message, state)
        assert "Выберите услугу" in message.answer.call_args[0][0]
        
        # cb_manual_book_service
        callback.data = f"manual_srv:{srv.id}"
        callback.message.answer.reset_mock()
        state.get_data.return_value = {
            "manual_book_student_id": 10,
            "manual_book_tutor_id": 1,
            "manual_book_time": (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        }
        with patch("app.services.booking_service.create_booking_internal") as mock_create:
            mock_booking = Booking(id=99, tutor_id=1, student_id=10, service_type="Тест", appointment_time=datetime.now())
            mock_booking.student = await db_session.get(Student, 10)
            mock_create.return_value = mock_booking
            await cb_manual_book_service(callback, state)
            assert "Занятие создано" in callback.message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_student_actions(db_session):
    b = Booking(
        id=77,
        tutor_id=1,
        student_id=10,
        service_type="Консультация",
        appointment_time=datetime.now(timezone.utc) + timedelta(hours=2),
        price=1500,
        status=BookingStatus.PENDING,
    )
    db_session.add(b)
    await db_session.commit()
    
    callback = MockCallbackQuery(
        from_user=MagicMock(id=1000),
        data="student_cancel_init:77",
        message=MockMessage(
            answer=AsyncMock(),
        ),
    )
    
    # Seed an AvailabilitySlot for the student reschedule test
    future_time = datetime.now(timezone.utc) + timedelta(days=10)
    slot = AvailabilitySlot(tutor_id=1, weekday=future_time.weekday(), start_time=time(0,0), end_time=time(23,59))
    db_session.add(slot)
    await db_session.commit()
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_student_cancel_init(callback)
        assert callback.message.answer.call_count == 1
        
        # cb_student_cancel_confirm
        callback.data = "student_cancel_confirm:77"
        callback.message.edit_text = AsyncMock()
        with patch("app.services.google_calendar_service.delete_calendar_event", AsyncMock()):
            await cb_student_cancel_confirm(callback)
            assert "отменено" in callback.message.edit_text.call_args[0][0]
            
        # cb_student_cancel_abort
        callback.message.edit_text.reset_mock()
        await cb_student_cancel_abort(callback)
        assert "отклонена" in callback.message.edit_text.call_args[0][0]
        
        # cb_student_reschedule_init
        b2 = Booking(
            id=78,
            tutor_id=1,
            student_id=10,
            service_type="Консультация",
            appointment_time=datetime.now(timezone.utc) + timedelta(hours=2),
            price=1500,
            status=BookingStatus.PENDING,
        )
        db_session.add(b2)
        await db_session.commit()
        
        callback.data = "student_reschedule_init:78"
        callback.message.answer.reset_mock()
        await cb_student_reschedule_init(callback, AsyncMock())
        assert callback.message.answer.call_count == 1
        
        # process_student_reschedule_datetime
        message = MockMessage(
            from_user=MagicMock(id=1000),
            text="invalid",
            answer=AsyncMock(),
        )
        state = AsyncMock()
        await process_student_reschedule_datetime(message, state)
        assert "Неверный формат" in message.answer.call_args[0][0]
        
        message.text = future_time.strftime("%d.%m.%Y %H:%M")
        state.get_data.return_value = {"student_reschedule_booking_id": 78}
        
        with patch("app.services.booking_service.check_availability", AsyncMock()), \
             patch("app.services.booking_service.check_tutor_absence", AsyncMock()), \
             patch("app.services.booking_service.check_double_booking", AsyncMock()):
            await process_student_reschedule_datetime(message, state)
            assert "отправлен" in message.answer.call_args[0][0]
        
        # cb_student_toggle_remind
        callback.data = "student_toggle_remind"
        callback.message.edit_reply_markup = AsyncMock()
        await cb_student_toggle_remind(callback)
        assert callback.message.edit_reply_markup.call_count == 1


# ── Broadcast & P2P & Extend Sub ──────────────────────────────────────

@pytest.mark.asyncio
async def test_broadcast_flow(db_session):
    message = MockMessage(
        from_user=MagicMock(id=111),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        state.get_data.return_value = {"tutor_id": 1}
        await cmd_broadcast(message, state)
        state.set_state.assert_called_with(BroadcastStates.composing)
        
        message.text = "Hello students!"
        await process_broadcast_text(message, state)
        state.update_data.assert_called_with(broadcast_text="Hello students!")
        
        callback = MockCallbackQuery(
            from_user=MagicMock(id=111),
            message=MockMessage(
                edit_text=AsyncMock(),
            ),
        )
        
        state.get_data = AsyncMock(return_value={"broadcast_text": "Hello students!", "tutor_id": 1})
        
        with patch("app.core.bot.get_bot") as mock_get_bot:
            mock_bot = AsyncMock()
            mock_get_bot.return_value = mock_bot
            await cb_broadcast_confirm(callback, state)
            assert callback.message.edit_text.call_count == 2
            
        callback.message.edit_text.reset_mock()
        await cb_broadcast_cancel(callback, state)
        state.clear.assert_called()
        assert "отменена" in callback.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_p2p_callbacks(db_session):
    b = Booking(
        id=55,
        tutor_id=1,
        student_id=10,
        service_type="Консультация",
        appointment_time=datetime.now(timezone.utc) + timedelta(hours=2),
        price=1500,
        status=BookingStatus.PENDING,
        payment_method="transfer",
    )
    db_session.add(b)
    await db_session.commit()
    
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        data="confirm_p2p:55",
        message=MockMessage(
            edit_text=AsyncMock(),
        ),
    )
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.services.google_calendar_service.sync_booking_to_calendar", AsyncMock()):
        await cb_confirm_p2p(callback)
        assert "Оплата подтверждена" in callback.message.edit_text.call_args[0][0]
        
    db_session.expunge_all()
    booking = await db_session.get(Booking, 55)
    assert booking.status == BookingStatus.CONFIRMED
    
    b2 = Booking(
        id=56,
        tutor_id=1,
        student_id=10,
        service_type="Консультация",
        appointment_time=datetime.now(timezone.utc) + timedelta(hours=2),
        price=1500,
        status=BookingStatus.PENDING,
        payment_method="transfer",
    )
    db_session.add(b2)
    await db_session.commit()
    
    callback.data = "cancel_p2p:56"
    callback.message.edit_text.reset_mock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_cancel_p2p(callback)
        assert "запись отклонена" in callback.message.edit_text.call_args[0][0]


@pytest.mark.asyncio
async def test_cash_callbacks(db_session):
    b = Booking(
        id=65,
        tutor_id=1,
        student_id=10,
        service_type="Консультация",
        appointment_time=datetime.now(timezone.utc) + timedelta(hours=2),
        price=1500,
        status=BookingStatus.PENDING,
        payment_method="cash",
    )
    db_session.add(b)
    await db_session.commit()
    
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        data="confirm_p2p:65",
        message=MockMessage(
            edit_text=AsyncMock(),
        ),
    )
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.services.google_calendar_service.sync_booking_to_calendar", AsyncMock()):
        await cb_confirm_p2p(callback)
        assert "Запись подтверждена" in callback.message.edit_text.call_args[0][0]
        
    db_session.expunge_all()
    booking = await db_session.get(Booking, 65)
    assert booking.status == BookingStatus.CONFIRMED
    
    b2 = Booking(
        id=66,
        tutor_id=1,
        student_id=10,
        service_type="Консультация",
        appointment_time=datetime.now(timezone.utc) + timedelta(hours=2),
        price=1500,
        status=BookingStatus.PENDING,
        payment_method="cash",
    )
    db_session.add(b2)
    await db_session.commit()
    
    callback.data = "cancel_p2p:66"
    callback.message.edit_text.reset_mock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_cancel_p2p(callback)
        assert "Запись отклонена" in callback.message.edit_text.call_args[0][0]



@pytest.mark.asyncio
async def test_cmd_extend_sub(db_session):
    from app.core.config import settings
    
    message = MockMessage(
        from_user=MagicMock(id=9999),
        text="/extend_sub 1 30",
        answer=AsyncMock(),
    )
    
    with patch.object(settings, "admin_tg_id", 55555):
        await cmd_extend_sub(message)
        assert "доступна только администратору" in message.answer.call_args[0][0]
        
    message.answer.reset_mock()
    message.text = "/extend_sub"
    with patch.object(settings, "admin_tg_id", 9999), \
         patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_extend_sub(message)
        assert "Использование" in message.answer.call_args[0][0]
        
        message.answer.reset_mock()
        message.text = "/extend_sub 1 30"
        await cmd_extend_sub(message)
        assert "Подписка продлена" in message.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_tutor_subscription_and_admin_flow(db_session):
    from app.bot.handlers import (
        cb_tutor_sub_pay,
        process_tutor_sub_payer_name,
        cb_admin_sub_approve,
        cb_admin_sub_reject,
        cmd_admin_panel,
        cb_admin_sub_give_manual,
        process_admin_sub_give_manual,
        TutorSubStates,
        AdminSubManualStates
    )
    from app.core.config import settings

    # 1. Test cb_tutor_sub_pay
    callback = MockCallbackQuery(
        from_user=MagicMock(id=88877),
        message=MockMessage(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    
    with patch.object(settings, "admin_sbp_phone", "+79112223344"), \
         patch.object(settings, "admin_sbp_bank", "Т-Банк"):
        await cb_tutor_sub_pay(callback, state)
        
        # Verify state set
        state.set_state.assert_called_with(TutorSubStates.waiting_payer_name)
        # Verify SBP details output
        assert "+79112223344" in callback.message.answer.call_args[0][0]
        assert "Т-Банк" in callback.message.answer.call_args[0][0]

    # 2. Test process_tutor_sub_payer_name
    message = MockMessage(
        from_user=MagicMock(id=88877, username="test_tutor_username"),
        text="Иванов И.",
        answer=AsyncMock(),
    )
    state = AsyncMock()
    
    # Pre-seed tutor in DB session
    tutor = Tutor(tg_id=88877, name="Tutor Test", is_active=True)
    db_session.add(tutor)
    await db_session.commit()
    tutor_id = tutor.id
    
    mock_bot = AsyncMock()
    with patch.object(settings, "admin_tg_id", 9999), \
         patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.core.bot.get_bot", return_value=mock_bot):
         
        await process_tutor_sub_payer_name(message, state)
        
        # Verify state cleared
        state.clear.assert_called_once()
        # Verify notification sent to admin
        mock_bot.send_message.assert_called_once()
        admin_text = mock_bot.send_message.call_args[1]["text"]
        assert "Иванов И." in admin_text
        assert "Tutor Test" in admin_text
        assert "9999" == str(mock_bot.send_message.call_args[1]["chat_id"])

    # 3. Test cb_admin_sub_approve
    callback_app = MockCallbackQuery(
        from_user=MagicMock(id=9999), # Admin
        data=f"admin_sub_app:{tutor_id}",
        message=MockMessage(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    
    with patch.object(settings, "admin_tg_id", 9999), \
         patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.core.bot.get_bot", return_value=mock_bot):
         
        await cb_admin_sub_approve(callback_app)
        
        # Verify DB updated
        await db_session.refresh(tutor)
        assert tutor.subscription_status == "active"
        assert tutor.subscription_expires_at is not None
        
        # Verify admin message edited
        assert "одобрена" in callback_app.message.edit_text.call_args[0][0]

    # 4. Test cb_admin_sub_reject
    callback_rej = MockCallbackQuery(
        from_user=MagicMock(id=9999), # Admin
        data=f"admin_sub_rej:{tutor_id}",
        message=MockMessage(edit_text=AsyncMock()),
        answer=AsyncMock(),
    )
    
    with patch.object(settings, "admin_tg_id", 9999), \
         patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.core.bot.get_bot", return_value=mock_bot):
         
        await cb_admin_sub_reject(callback_rej)
        # Verify message edited
        assert "отклонена" in callback_rej.message.edit_text.call_args[0][0]

    # 5. Test cmd_admin_panel (Admin only)
    message_admin = MockMessage(
        from_user=MagicMock(id=9999),
        text="/admin",
        answer=AsyncMock(),
    )
    with patch.object(settings, "admin_tg_id", 9999), \
         patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_admin_panel(message_admin)
        admin_response = message_admin.answer.call_args[0][0]
        assert "Панель администратора" in admin_response
        assert "Список репетиторов:" in admin_response
        assert "Tutor Test" in admin_response
        assert f"ID: {tutor_id}" in admin_response or f"ID: <code>{tutor_id}</code>" in admin_response

    # 6. Test process_admin_sub_give_manual
    message_give = MockMessage(
        from_user=MagicMock(id=9999),
        text=f"{tutor_id} 45", # Extend tutor by 45 days
        answer=AsyncMock(),
    )
    state_give = AsyncMock()
    with patch.object(settings, "admin_tg_id", 9999), \
         patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.core.bot.get_bot", return_value=mock_bot):
        await process_admin_sub_give_manual(message_give, state_give)
        
        await db_session.refresh(tutor)
        # Check added days
        assert tutor.subscription_status == "active"
        # State cleared
        state_give.clear.assert_called_once()
        # Verify success message
        assert "Подписка успешно выдана" in message_give.answer.call_args[0][0]


@pytest.mark.asyncio
async def test_admin_sub_revoke_flow(db_session):
    from app.bot.handlers import (
        cmd_revoke_sub,
        cb_admin_sub_revoke_manual,
        process_admin_sub_revoke_manual,
        AdminSubManualStates
    )
    from app.core.config import settings

    # Pre-seed tutor in DB session
    tutor = Tutor(tg_id=88899, name="Tutor Revoke Test", is_active=True, subscription_status="active", subscription_expires_at=datetime.now(timezone.utc)+timedelta(days=10))
    db_session.add(tutor)
    await db_session.commit()
    tutor_id = tutor.id

    mock_bot = AsyncMock()

    # 1. Test cmd_revoke_sub (Admin only)
    message_rev = MockMessage(
        from_user=MagicMock(id=9999),
        text=f"/revoke_sub {tutor_id}",
        answer=AsyncMock(),
    )
    with patch.object(settings, "admin_tg_id", 9999), \
         patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.core.bot.get_bot", return_value=mock_bot):
        await cmd_revoke_sub(message_rev)
        
        await db_session.refresh(tutor)
        assert tutor.subscription_status == "expired"
        assert "успешно аннулирована" in message_rev.answer.call_args[0][0]
        
        # Verify ReplyKeyboardRemove was sent to tutor
        mock_bot.send_message.assert_called_once()
        sent_markup = mock_bot.send_message.call_args[1].get("reply_markup")
        from aiogram.types import ReplyKeyboardRemove
        assert isinstance(sent_markup, ReplyKeyboardRemove)

    # Re-enable sub for manual revoke test
    tutor.subscription_status = "active"
    tutor.subscription_expires_at = datetime.now(timezone.utc) + timedelta(days=10)
    await db_session.commit()

    # 2. Test cb_admin_sub_revoke_manual
    callback = MockCallbackQuery(
        from_user=MagicMock(id=9999),
        message=MockMessage(answer=AsyncMock()),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch.object(settings, "admin_tg_id", 9999):
        await cb_admin_sub_revoke_manual(callback, state)
        state.set_state.assert_called_with(AdminSubManualStates.waiting_tutor_id_revoke)

    # 3. Test process_admin_sub_revoke_manual
    message_manual = MockMessage(
        from_user=MagicMock(id=9999),
        text=f"{tutor_id}",
        answer=AsyncMock(),
    )
    with patch.object(settings, "admin_tg_id", 9999), \
         patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)), \
         patch("app.core.bot.get_bot", return_value=mock_bot):
        mock_bot.send_message.reset_mock()
        await process_admin_sub_revoke_manual(message_manual, state)
        
        await db_session.refresh(tutor)
        assert tutor.subscription_status == "expired"
        state.clear.assert_called_once()
        assert "успешно аннулирована" in message_manual.answer.call_args[0][0]
        
        # Verify ReplyKeyboardRemove was sent to tutor
        mock_bot.send_message.assert_called_once()
        sent_markup = mock_bot.send_message.call_args[1].get("reply_markup")
        from aiogram.types import ReplyKeyboardRemove
        assert isinstance(sent_markup, ReplyKeyboardRemove)


# ── universal callback detail & toggle ───────────────────────────────

@pytest.mark.asyncio
async def test_universal_callbacks(db_session):
    b = Booking(
        id=90,
        tutor_id=1,
        student_id=10,
        service_type="Консультация",
        appointment_time=datetime.now(timezone.utc) + timedelta(hours=2),
        price=1500,
        status=BookingStatus.PENDING,
    )
    db_session.add(b)
    await db_session.commit()
    
    callback = MockCallbackQuery(
        from_user=MagicMock(id=111),
        data="detail:90",
        message=MockMessage(
            answer=AsyncMock(),
        ),
    )
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cb_detail(callback)
        assert callback.message.answer.call_count == 1
        
        callback.data = "toggle:1"
        callback.message.edit_text = AsyncMock()
        await cb_toggle(callback)
        assert callback.message.edit_text.call_count == 1


# ── Additional Coverage Tests ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_tutor_callback_middleware_success(db_session):
    middleware = TutorCallbackMiddleware()
    handler = AsyncMock(return_value="SUCCESS")
    event = MockCallbackQuery(
        data="confirm:10",
        from_user=MagicMock(id=111),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        res = await middleware(handler, event, {})
        assert res == "SUCCESS"


@pytest.mark.asyncio
async def test_tutor_message_middleware_caching(db_session):
    middleware = TutorMessageMiddleware()
    handler = AsyncMock(return_value="SUCCESS")
    
    # Non-tutor user
    event = MockMessage(
        from_user=MagicMock(id=99999, username=None),
        text="📅 Расписание",
        answer=AsyncMock(),
    )
    
    from app.bot.handlers import _tutor_cache
    _tutor_cache.clear()
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        # First call: hits DB and caches as False
        res1 = await middleware(handler, event, {})
        assert res1 == "SUCCESS"  # passed to handler since non-tutor
        
        # Second call: hits cache (is_tutor_cached is False)
        res2 = await middleware(handler, event, {})
        assert res2 == "SUCCESS"


@pytest.mark.asyncio
async def test_build_student_menu_empty():
    from app.bot.handlers import build_student_menu
    kb = build_student_menu(tutor_ids=None)
    assert kb is not None


@pytest.mark.asyncio
async def test_cmd_start_ref_invalid(db_session):
    message = MockMessage(
        from_user=MagicMock(id=99999, username=None),
        text="/start ref_invalid",
        answer=AsyncMock(),
    )
    state = AsyncMock()
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await cmd_start(message, state)
        assert message.answer.call_count >= 1


@pytest.mark.asyncio
async def test_handle_non_tutor_fallback(db_session):
    message = MockMessage(
        from_user=MagicMock(id=99999, username=None),
        text="some regular text",
        answer=AsyncMock(),
    )
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await _handle_non_tutor(message, db_session)
        assert any("пройти регистрацию" in call[0][0] for call in message.answer.call_args_list)


@pytest.mark.asyncio
async def test_student_reg_existing_phone_new_link(db_session):
    # Student 10 is Linked Student (linked to tutor 1)
    # Let's register student 10 to tutor 3 (who exists but has no link to student 10)
    message = MockMessage(
        contact=MagicMock(phone_number="+79001112233"),
        from_user=MagicMock(id=1000, username="student_linked"),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    state.get_data.return_value = {"reg_full_name": "Linked Student Updated", "reg_tutor_id": 3}
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await process_student_reg_phone(message, state)
        assert any("успешно завершена" in call[0][0] for call in message.answer.call_args_list)
        
        # Verify link with tutor 3 is created and active
        link = await db_session.get(StudentTutorLink, (10, 3))
        assert link is not None
        assert link.is_active is True


@pytest.mark.asyncio
async def test_student_reg_existing_phone_inactive_link(db_session):
    # Student 20 is Unlinked Student, who has is_active=False with tutor 1
    # Try to register student 20 to tutor 1
    message = MockMessage(
        contact=MagicMock(phone_number="+79002223344"),
        from_user=MagicMock(id=2000, username="student_unlinked"),
        answer=AsyncMock(),
    )
    state = AsyncMock()
    state.get_data.return_value = {"reg_full_name": "Unlinked Student", "reg_tutor_id": 1}
    
    with patch("app.bot.handlers.async_session_factory", return_value=MockAsyncSessionContext(db_session)):
        await process_student_reg_phone(message, state)
        assert any("удалены репетитором из базы" in call[0][0] for call in message.answer.call_args_list)

