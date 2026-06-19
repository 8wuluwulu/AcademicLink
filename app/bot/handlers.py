"""
AcademicLink — Bot Handlers (Scheduling CRM)

Tutor dashboard & workflow:
  /start · 🏠 Главная — dynamic dashboard with profile, today's count, pending
  📅 Расписание        — paginated PENDING+CONFIRMED grouped by Date → Student
  👥 Ученики           — FSM student search by phone
  ⚙️ Настройки         — profile + is_active toggle
  ◀️ Назад             — universal back to main menu
  /today               — daily briefing
  Callbacks            — confirm, cancel (with reason FSM), detail, toggle, page
"""

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.bot.formatting import (
    BACK_KB,
    MAIN_MENU,
    MSK,
    PAGE_SIZE,
    STATUS_EMOJI,
    STATUS_LABEL,
    build_booking_actions,
    build_page_nav,
    fmt_booking_compact,
    fmt_contact_links,
    fmt_date,
    fmt_date_dot,
    fmt_full,
    fmt_time,
)

from app.db.database import async_session_factory
from app.db.models import AvailabilitySlot, Booking, BookingStatus, Service, Student, Tutor, TutorAbsence, StudentTutorLink

logger = logging.getLogger(__name__)
router = Router(name="main_router")




# ── FSM States ───────────────────────────────────────────────────────


class StudentSearch(StatesGroup):
    waiting_phone = State()


class StudentManagement(StatesGroup):
    confirm_delete = State()


class TutorAbsenceStates(StatesGroup):
    waiting_start = State()
    waiting_end = State()


class SlotManagement(StatesGroup):
    entering_times = State()


class BroadcastStates(StatesGroup):
    composing = State()


class RescheduleStates(StatesGroup):
    waiting_datetime = State()


class StudentRescheduleStates(StatesGroup):
    waiting_datetime = State()


class ManualBookingStates(StatesGroup):
    waiting_datetime = State()
    waiting_service_type = State()


class TutorSettingsStates(StatesGroup):
    waiting_meeting_link = State()
    waiting_sbp_phone = State()
    waiting_sbp_bank = State()
    waiting_sbp_link = State()
    waiting_sbp_qr = State()
    waiting_name = State()


class StudentSettingsStates(StatesGroup):
    waiting_name = State()
    waiting_phone = State()


class ServiceManagement(StatesGroup):
    waiting_name = State()
    waiting_duration = State()
    waiting_buffer = State()
    waiting_price = State()
    
    # Editing states
    editing_service_id = State()
    editing_name = State()
    editing_duration = State()
    editing_buffer = State()
    editing_price = State()


class StudentRegistrationStates(StatesGroup):
    waiting_full_name = State()
    waiting_phone = State()


class TutorRegistrationStates(StatesGroup):
    waiting_full_name = State()


class TutorSubStates(StatesGroup):
    waiting_payer_name = State()



# ── Helpers ──────────────────────────────────────────────────────────


async def _get_tutor(tg_id: int, session) -> Tutor | None:
    result = await session.execute(select(Tutor).where(Tutor.tg_id == tg_id))
    return result.scalar_one_or_none()


def build_student_menu(
    tutor_ids: list[int] | None,
    tg_username: str | None = None,
    tg_id: int | None = None,
) -> ReplyKeyboardMarkup:
    if tutor_ids is None:
        tutor_ids = []

    rows = []
    if tutor_ids:
        from app.core.config import settings
        from urllib.parse import urlencode
        
        params = {}
        if tg_username:
            params["tg_username"] = tg_username
        if tg_id:
            params["tg_id"] = str(tg_id)
            
        query_str = f"?{urlencode(params)}" if params else ""
        web_app_url = f"{settings.web_url}/book/{tutor_ids[0]}{query_str}"
        rows.append([
            KeyboardButton(text="📅 Записаться", web_app=WebAppInfo(url=web_app_url))
        ])
        
    rows.append([
        KeyboardButton(text="🗂 Мои записи"),
        KeyboardButton(text="⚙️ Настройки")
    ])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…"
    )


from aiogram import BaseMiddleware
from typing import Callable, Dict, Any, Awaitable
import time as _time

# BUG #014 fix: simple in-memory TTL cache for tutor lookups in middleware
_tutor_cache: dict[int, tuple[float, bool]] = {}  # tg_id -> (timestamp, is_tutor)
_TUTOR_CACHE_TTL = 300  # 5 minutes

class TutorCallbackMiddleware(BaseMiddleware):
    """Secures all callback queries: only registered tutors, only active subscriptions."""
    async def __call__(
        self,
        handler: Callable[[CallbackQuery, Dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: Dict[str, Any]
    ) -> Any:
        # Allow student-scoped and admin-scoped callbacks to pass through without tutor check
        if event.data and (event.data.startswith("student_") or event.data.startswith("admin_sub_")):
            return await handler(event, data)


        async with async_session_factory() as session:
            tutor = await _get_tutor(event.from_user.id, session)
            if tutor is None:
                await event.answer("⚠️ Это действие доступно только репетиторам.", show_alert=True)
                return
            # Allow subscription and admin callbacks through even for expired subscriptions
            bypass_prefixes = ("tutor_sub_", "admin_sub_")
            is_bypassed = any(event.data.startswith(p) for p in bypass_prefixes) if event.data else False
            from app.core.config import settings
            if event.from_user.id == settings.admin_tg_id:
                is_bypassed = True

            if not is_bypassed:
                sub_expires = tutor.subscription_expires_at
                if sub_expires and sub_expires.tzinfo is None:
                    sub_expires = sub_expires.replace(tzinfo=timezone.utc)
                if sub_expires is None or sub_expires < datetime.now(timezone.utc):
                    await event.answer(
                        "⚠️ Ваша подписка на AcademicLink истекла. "
                        "Отправьте любое текстовое сообщение в чат бота, чтобы получить кнопку оплаты.",
                        show_alert=True,
                    )
                    return
            # BUG #003 fix: inject tutor object for P2P ownership checks in handlers
            data["_middleware_tutor"] = tutor
        return await handler(event, data)

router.callback_query.outer_middleware(TutorCallbackMiddleware())


class TutorMessageMiddleware(BaseMiddleware):
    """Secures all command and message inputs from tutors: blocks if subscription is expired/not set."""
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        from app.core.config import settings
        is_admin = settings.admin_tg_id is not None and event.from_user.id == settings.admin_tg_id
        is_start = event.text and event.text.startswith("/start")

        is_sub_fsm = False
        state = data.get("state")
        if state:
            current_state = await state.get_state()
            if current_state and current_state.startswith("TutorSubStates"):
                is_sub_fsm = True

        if not is_admin and not is_start and not is_sub_fsm:
            tg_id = event.from_user.id
            # BUG #014 fix: check TTL cache before hitting DB
            now_ts = _time.monotonic()
            cached = _tutor_cache.get(tg_id)
            is_tutor_cached = None
            if cached and (now_ts - cached[0]) < _TUTOR_CACHE_TTL:
                is_tutor_cached = cached[1]

            if is_tutor_cached is False:
                # Known non-tutor, skip DB query
                pass
            elif is_tutor_cached is True or is_tutor_cached is None:
                async with async_session_factory() as session:
                    tutor = await _get_tutor(tg_id, session)
                    _tutor_cache[tg_id] = (now_ts, tutor is not None)
                    if tutor is not None:
                        sub_expires = tutor.subscription_expires_at
                        if sub_expires and sub_expires.tzinfo is None:
                            sub_expires = sub_expires.replace(tzinfo=timezone.utc)
                        if sub_expires is None or sub_expires < datetime.now(timezone.utc):
                            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                            kb = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="💳 Оформить подписку (990 ₽/мес)", callback_data="tutor_sub_pay")]
                            ])
                            await event.answer(
                                "⚠️ <b>Ваша подписка на AcademicLink истекла!</b>\n\n"
                                "Для продления доступа к функциям расписания, пожалуйста, оформите подписку.",
                                parse_mode="HTML",
                                reply_markup=kb
                            )
                            return
        return await handler(event, data)

router.message.outer_middleware(TutorMessageMiddleware())


async def _handle_non_tutor(message: Message, session) -> None:
    """Handles messages from non-tutors gracefully by resetting their keyboard or showing onboarding."""
    tg_id = message.from_user.id
    student_stmt = select(StudentTutorLink.tutor_id).join(Student).where(
        Student.telegram_id == tg_id,
        StudentTutorLink.is_active == True,
    )
    student_res = await session.execute(student_stmt)
    tutor_ids = list(student_res.scalars().all())
    
    if tutor_ids:
        reply_kb = build_student_menu(tutor_ids, message.from_user.username, message.from_user.id)
        await message.answer(
            "⚠️ <b>Этот раздел доступен только репетиторам.</b>\n\n"
            "Вы зарегистрированы как ученик. Используйте кнопки меню ниже для управления вашими записями.",
            parse_mode="HTML",
            reply_markup=reply_kb
        )
    else:
        # Check if they are a student globally (deactivated by all tutors)
        student_stmt = select(Student).where(Student.telegram_id == tg_id).limit(1)
        student_res = await session.execute(student_stmt)
        linked_student = student_res.scalar_one_or_none()
        
        from aiogram.types import ReplyKeyboardRemove
        if linked_student:
            await message.answer(
                "❌ <b>Доступ ограничен</b>\n\n"
                "Вы были удалены из базы учеников. Запись на новые занятия и просмотр расписания недоступны.\n"
                "Пожалуйста, свяжитесь со своим преподавателем напрямую для восстановления доступа.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await message.answer(
                "👋 <b>Добро пожаловать в AcademicLink!</b>\n\n"
                "Вы зашли в бот системы записи и планирования занятий.\n\n"
                "⚠️ <b>Вы не зарегистрированы в системе.</b>\n"
                "Если вы ученик, пожалуйста, перейдите по специальной ссылке для записи, "
                "которую вам предоставил ваш репетитор, чтобы пройти регистрацию.\n\n"
                "Если вы репетитор, убедитесь, что ваш Telegram ID настроен корректно.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )


_NOT_REGISTERED = (
    "<b>Вы не зарегистрированы</b>\n\n"
    "Убедитесь, что ваш Telegram ID добавлен в "
    "<code>.env</code> как <code>DEFAULT_TUTOR_TG_ID</code>.\n\n"
    "Отправьте /start чтобы узнать свой ID."
)


def _greeting() -> str:
    h = datetime.now(MSK).hour
    if h < 6:
        return "Доброй ночи"
    if h < 12:
        return "Доброе утро"
    if h < 18:
        return "Добрый день"
    return "Добрый вечер"


# ═════════════════════════════════════════════════════════════════════
#  ◀️ Назад — universal back (registered BEFORE other text handlers)
# ═════════════════════════════════════════════════════════════════════


@router.message(F.text == "◀️ Назад")
async def cmd_back(message: Message, state: FSMContext) -> None:
    """Clear any FSM state and return to the main menu."""
    await state.clear()
    await _send_dashboard(message, state)


# ═════════════════════════════════════════════════════════════════════
#  🏠 Главная / /start — dynamic dashboard
# ═════════════════════════════════════════════════════════════════════


async def _send_dashboard(message: Message, state: FSMContext | None = None) -> None:
    """Build and send the Tutor Dashboard or Student Welcome."""
    tg_id = message.from_user.id
    username = message.from_user.username
    name = message.from_user.first_name or "Пользователь"
    now = datetime.now(MSK)

    async with async_session_factory() as session:
        tutor = await _get_tutor(tg_id, session)

        # ── 1. Handle Tutor Dashboard ────────────────────────────────
        if tutor:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)

            today_res = await session.execute(
                select(func.count(Booking.id)).where(
                    Booking.tutor_id == tutor.id,
                    Booking.status == BookingStatus.CONFIRMED,
                    Booking.appointment_time >= day_start.astimezone(timezone.utc),
                    Booking.appointment_time < day_end.astimezone(timezone.utc),
                )
            )
            today_confirmed = today_res.scalar_one()

            pending_res = await session.execute(
                select(func.count(Booking.id)).where(
                    Booking.tutor_id == tutor.id,
                    Booking.status == BookingStatus.PENDING,
                )
            )
            pending_count = pending_res.scalar_one()

            students_res = await session.execute(
                select(func.count(StudentTutorLink.student_id)).where(
                    StudentTutorLink.tutor_id == tutor.id,
                    StudentTutorLink.is_active == True,
                )
            )
            total_students = students_res.scalar_one()

            status_icon = "🟢" if tutor.is_active else "🔴"
            status_text = "Активен" if tutor.is_active else "Пауза"

            sub_banner = ""
            is_sub_expired = False
            if tutor.subscription_expires_at is None:
                is_sub_expired = True
                sub_banner = "⚠️ <b>Подписка не оформлена!</b> Расписание заблокировано.\n\n"
            else:
                sub_expires = tutor.subscription_expires_at
                if sub_expires.tzinfo is None:
                    sub_expires = sub_expires.replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                if sub_expires < now_utc:
                    is_sub_expired = True
                    sub_banner = "⚠️ <b>Подписка истекла!</b> Расписание заблокировано.\n\n"
                else:
                    days_left = (sub_expires - now_utc).days
                    if days_left <= 3:
                        sub_banner = f"⚠️ <b>Подписка истекает через {days_left} дн.!</b>\n\n"

            if is_sub_expired:
                text = (
                    f"{_greeting()}, <b>{tutor.name}</b>!\n\n"
                    f"👤 {tutor.name}  ·  {status_icon} {status_text}\n\n"
                    f"{sub_banner}"
                    f"Для восстановления доступа к расписанию и функциям бота, пожалуйста, оформите подписку."
                )
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оформить подписку (990 ₽/мес)", callback_data="tutor_sub_pay")]
                ])
                await message.answer("⚠️ Доступ заблокирован.", reply_markup=ReplyKeyboardRemove())
                await message.answer(text, parse_mode="HTML", reply_markup=kb)
                return

            text = (
                f"{_greeting()}, <b>{tutor.name}</b>!\n\n"
                f"👤 {tutor.name}  ·  {status_icon} {status_text}\n\n"
                f"{sub_banner}"
                f"📅 Подтверждено на сегодня: <b>{today_confirmed}</b>\n"
                f"🟡 Новые заявки: <b>{pending_count}</b>\n"
                f"👥 Всего учеников: <b>{total_students}</b>\n\n"
                "<i>Выберите действие из меню ниже:</i>"
            )
            
            await message.answer(text, parse_mode="HTML", reply_markup=MAIN_MENU)
            return

        # ── 1.5. Handle Already Linked Student ───────────────────────
        student_stmt = select(Student).where(Student.telegram_id == tg_id).limit(1)
        student_res = await session.execute(student_stmt)
        linked_student = student_res.scalar_one_or_none()
        student_stmt = select(StudentTutorLink.tutor_id).join(Student).where(
            Student.telegram_id == tg_id,
            StudentTutorLink.is_active == True,
        )
        student_res = await session.execute(student_stmt)
        tutor_ids = list(student_res.scalars().all())
        if tutor_ids:
            reply_kb = build_student_menu(tutor_ids, message.from_user.username, message.from_user.id)

            # Build reminder toggle inline button
            # BUG #013: guard against None linked_student
            remind_icon = "🔔" if (linked_student and linked_student.wants_reminders) else "🔕"
            remind_label = "Вкл" if (linked_student and linked_student.wants_reminders) else "Выкл"
            remind_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"{remind_icon} Напоминания: {remind_label}",
                    callback_data="student_toggle_remind",
                )]
            ])

            await message.answer(
                f"👋 Рады видеть вас снова, <b>{linked_student.full_name}</b>!\n\n"
                f"Вы вошли как ученик в системе <b>AcademicLink</b>. "
                f"Здесь вы будете получать напоминания о ваших занятиях.\n\n"
                f"Используйте кнопки меню ниже, чтобы записаться на новое занятие или посмотреть свои записи.",
                parse_mode="HTML",
                reply_markup=reply_kb
            )
            await message.answer(
                "⚙️ <b>Настройки уведомлений:</b>",
                parse_mode="HTML",
                reply_markup=remind_kb,
            )
            return
        elif linked_student:
            await message.answer(
                "❌ <b>Доступ ограничен</b>\n\n"
                "Вы были удалены из базы учеников. Запись на новые занятия и просмотр расписания недоступны.\n"
                "Пожалуйста, свяжитесь со своим преподавателем напрямую для восстановления доступа.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
            return

        # ── 2. Handle Student Automatic Linking ──────────────────────
        if username:
            stmt = select(Student).where(
                Student.telegram_username == username,
                Student.telegram_id.is_(None)
            )
            result = await session.execute(stmt)
            unlinked_students = result.scalars().all()

            if unlinked_students:
                for s in unlinked_students:
                    s.telegram_id = tg_id
                await session.commit()

                student_stmt = select(StudentTutorLink.tutor_id).join(Student).where(
                    Student.telegram_id == tg_id,
                    StudentTutorLink.is_active == True,
                )
                student_res = await session.execute(student_stmt)
                tutor_ids = list(student_res.scalars().all())
                reply_kb = build_student_menu(tutor_ids, message.from_user.username, message.from_user.id)

                await message.answer(
                    f"👋 Привет, <b>{unlinked_students[0].full_name}</b>!\n\n"
                    f"Я — бот системы <b>AcademicLink</b>. Теперь вы будете получать "
                    f"уведомления и напоминания о ваших занятиях прямо здесь.\n\n"
                    f"✅ Ваш профиль успешно привязан.\n"
                    f"Используйте кнопки меню ниже, чтобы записаться на занятие или посмотреть свои записи.",
                    parse_mode="HTML",
                    reply_markup=reply_kb
                )
                return

        # ── 3. Onboarding: Start Tutor Registration Flow ──
        if state is not None:
            await state.set_state(TutorRegistrationStates.waiting_full_name)
            await message.answer(
                "👋 <b>Добро пожаловать в AcademicLink!</b>\n\n"
                "Для завершения регистрации в качестве преподавателя, пожалуйста, введите ваши "
                "<b>Фамилию, Имя и Отчество</b> через пробел.\n\n"
                "<i>Например: Иванов Иван Иванович</i>",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await message.answer("Пожалуйста, отправьте /start для начала регистрации.")
        return


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()

    # Check for referral start parameter (e.g., ref_1)
    if message.text and len(message.text.split()) > 1:
        param = message.text.split()[1]
        if param.startswith("ref_"):
            try:
                tutor_id = int(param.split("_")[1])
                await start_student_registration(message, state, tutor_id)
                return
            except (ValueError, IndexError):
                pass

    if message.text and "gcal_success" in message.text:
        await message.answer("🎉 <b>Google Календарь успешно подключен!</b>\n\nТеперь все новые записи будут автоматически появляться в вашем календаре, а занятые слоты будут блокироваться на лендинге.", parse_mode="HTML")
    await _send_dashboard(message, state)


@router.message(F.text == "🏠 Главная")
async def cmd_home(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_dashboard(message, state)


# ── Tutor Registration FSM Handler ───────────────────────────────────

@router.message(TutorRegistrationStates.waiting_full_name)
async def process_tutor_registration_name(message: Message, state: FSMContext) -> None:
    full_name = message.text.strip()
    parts = full_name.split()
    if len(parts) < 2 or len(full_name) < 3 or not all(c.isalpha() or c.isspace() or c in "-." for c in full_name):
        await message.answer(
            "❌ <b>Пожалуйста, введите ваши корректные Фамилию, Имя и Отчество.</b>\n\n"
            "Используйте буквы русского или латинского алфавита и пробелы (минимум 2 слова).\n\n"
            "<i>Пример: Иванов Иван Иванович</i>",
            parse_mode="HTML"
        )
        return

    tg_id = message.from_user.id
    
    from datetime import time
    from app.core.config import settings
    from app.db.models import Service, AvailabilitySlot

    async with async_session_factory() as session:
        tutor = Tutor(
            tg_id=tg_id,
            name=full_name,
            is_active=True,
            lesson_duration=60,
            buffer_time=15,
            subscription_expires_at=datetime.now(timezone.utc) + timedelta(days=30),
            subscription_status="trial",
        )
        session.add(tutor)
        await session.flush()  # gets tutor.id

        # Create default Service
        default_service = Service(
            tutor_id=tutor.id,
            name="Консультация",
            duration=60,
            buffer_time=15,
            price=1500,
            is_active=True,
        )
        session.add(default_service)

        # Create default availability slots (Mon-Fri 09:00 - 18:00)
        for day in range(5):  # 0 = Monday, 4 = Friday
            slot = AvailabilitySlot(
                tutor_id=tutor.id,
                weekday=day,
                start_time=time(9, 0),
                end_time=time(18, 0),
            )
            session.add(slot)

        await session.commit()

        from app.core.bot import get_bot_username
        bot_username = get_bot_username()

        # Beautiful greeting message with personal link and button
        text = (
            f"🎉 <b>Добро пожаловать в AcademicLink, {full_name}!</b>\n\n"
            f"Вы успешно зарегистрированы в качестве репетитора. Вам активирован бесплатный пробный период на 30 дней.\n\n"
            f"🤖 <b>Ваша ссылка для записи через Telegram:</b>\n"
            f"<code>https://t.me/{bot_username}?start=ref_{tutor.id}</code>\n\n"
            f"⚙️ <b>Настройки:</b>\n"
            f"Вы можете настроить свои услуги, время работы и календарь с помощью меню.\n"
            f"Давайте подключим Google Календарь для синхронизации!"
        )
        
        is_localhost = "localhost" in settings.web_url or "127.0.0.1" in settings.web_url
        
        gcal_onboarding_btn = (
            InlineKeyboardButton(text="🔗 Подключить Google Календарь", callback_data="gcal_localhost_warning")
            if is_localhost
            else InlineKeyboardButton(text="🔗 Подключить Google Календарь", url=f"{settings.web_url}/api/v1/auth/google/login/{tutor.id}")
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[gcal_onboarding_btn]])
        
        await message.answer(text, parse_mode="HTML", reply_markup=MAIN_MENU)
        await message.answer("Рекомендуем сразу настроить интеграцию:", reply_markup=kb)

    await state.clear()
    logger.info("Tutor tg_id=%d registered with name: %s", tg_id, full_name)



# ── Student Registration Flow (Deep Linking) ─────────────────────────

async def start_student_registration(message: Message, state: FSMContext, tutor_id: int) -> None:
    tg_id = message.from_user.id
    async with async_session_factory() as session:
        # Prevent tutor from registering as their own student
        tutor = await session.get(Tutor, tutor_id)
        if not tutor:
            await message.answer("❌ <b>Преподаватель не найден.</b>", parse_mode="HTML")
            return
        if tutor.tg_id == tg_id:
            await message.answer(
                "⚠️ <b>Вы перешли по собственной ссылке для записи учеников!</b>\n\n"
                "Бот не может зарегистрировать вас как вашего собственного ученика.\n"
                "Отправьте эту ссылку вашему ученику или откройте её с другого аккаунта Telegram для тестирования.",
                parse_mode="HTML"
            )
            return

        # Check if already registered student for this specific tutor
        stmt = select(StudentTutorLink).join(Student).where(
            Student.telegram_id == tg_id,
            StudentTutorLink.tutor_id == tutor_id
        )
        res = await session.execute(stmt)
        link = res.scalar_one_or_none()
        
        if link:
            if not link.is_active:
                await message.answer(
                    "❌ <b>Доступ ограничен</b>\n\n"
                    "Вы были удалены этим преподавателем из базы учеников. Запись недоступна.\n"
                    "Пожалуйста, свяжитесь с преподавателем напрямую для восстановления доступа.",
                    parse_mode="HTML",
                    reply_markup=ReplyKeyboardRemove()
                )
                return
            # Student is already registered and active with this specific tutor! Just show the welcome dashboard
            await _send_dashboard(message)
            return

        # Check if registered with ANY OTHER tutor
        stmt = select(Student).where(Student.telegram_id == tg_id)
        res = await session.execute(stmt)
        student_global = res.scalar_one_or_none()
        
        if student_global:
            # Under Many-to-Many we just insert a new StudentTutorLink record!
            new_link = StudentTutorLink(
                student_id=student_global.id,
                tutor_id=tutor_id,
                is_active=True,
            )
            session.add(new_link)
            await session.commit()
            
            # Fetch all tutors linked to this student
            tutor_stmt = select(StudentTutorLink.tutor_id).join(Student).where(
                Student.telegram_id == tg_id,
                StudentTutorLink.is_active == True,
            )
            tutor_res = await session.execute(tutor_stmt)
            tutor_ids = list(tutor_res.scalars().all())
            reply_kb = build_student_menu(tutor_ids, message.from_user.username, message.from_user.id)
            
            await message.answer(
                f"🎉 <b>Вы успешно прикрепились к преподавателю {tutor.name}!</b>\n\n"
                f"Ваши данные (имя и телефон) скопированы автоматически.\n"
                f"Используйте кнопки меню ниже, чтобы записаться на новое занятие или посмотреть свои записи.",
                parse_mode="HTML",
                reply_markup=reply_kb
            )
            return

        # Store tutor_id in state
        await state.update_data(reg_tutor_id=tutor_id)
        
    await state.set_state(StudentRegistrationStates.waiting_full_name)
    await message.answer(
        f"👋 <b>Добро пожаловать в AcademicLink!</b>\n\n"
        f"Давайте зарегистрируем вас как ученика, чтобы вы могли записываться на занятия и получать напоминания.\n\n"
        f"👤 <b>Введите ваши Имя и Фамилию:</b>",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


@router.message(StudentRegistrationStates.waiting_full_name)
async def process_student_reg_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if len(name) < 2 or len(name) > 100:
        await message.answer("❌ Пожалуйста, введите корректные Имя и Фамилию (от 2 до 100 символов).")
        return
    
    await state.update_data(reg_full_name=name)
    await state.set_state(StudentRegistrationStates.waiting_phone)
    
    await message.answer(
        "📞 <b>Отлично! Теперь пришлите ваш номер телефона.</b>\n\n"
        "Пожалуйста, введите его в международном формате (например, <code>+79001234567</code>).",
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )


@router.message(StudentRegistrationStates.waiting_phone, F.contact | F.text)
async def process_student_reg_phone(message: Message, state: FSMContext) -> None:
    import re
    phone_input = ""
    if message.contact:
        phone_input = message.contact.phone_number
    else:
        phone_input = message.text.strip()

    digits = "".join(c for c in phone_input if c.isdigit())
    
    if len(digits) == 11 and (digits.startswith("89") or digits.startswith("79")):
        phone = "+79" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        phone = "+79" + digits[1:]
    else:
        await message.answer(
            "❌ Номер телефона должен быть мобильным номером РФ (например, +79109215428).",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    full_name = data["reg_full_name"]
    tutor_id = data["reg_tutor_id"]
    tg_id = message.from_user.id
    username = message.from_user.username

    async with async_session_factory() as session:
        # Check if student with this phone already exists globally in DB
        stmt = select(Student).where(Student.phone == phone)
        res = await session.execute(stmt)
        student = res.scalar_one_or_none()

        if student is None:
            # Create brand new student
            student = Student(
                full_name=full_name,
                phone=phone,
                telegram_id=tg_id,
                telegram_username=username,
            )
            session.add(student)
            await session.flush()
            
            # Create a tutor link
            link = StudentTutorLink(
                student_id=student.id,
                tutor_id=tutor_id,
                is_active=True,
            )
            session.add(link)
        else:
            # Link/Update existing student record
            student.telegram_id = tg_id
            student.full_name = full_name
            if username:
                student.telegram_username = username
                
            # Check or create link
            link_stmt = select(StudentTutorLink).where(
                StudentTutorLink.student_id == student.id,
                StudentTutorLink.tutor_id == tutor_id
            )
            link_res = await session.execute(link_stmt)
            link = link_res.scalar_one_or_none()
            if link is None:
                link = StudentTutorLink(
                    student_id=student.id,
                    tutor_id=tutor_id,
                    is_active=True,
                )
                session.add(link)
            else:
                if not link.is_active:
                    await message.answer("❌ Вы были удалены репетитором из базы. Запись недоступна.")
                    await state.clear()
                    return

        await session.commit()
        
        student_stmt = select(StudentTutorLink.tutor_id).join(Student).where(
            Student.telegram_id == tg_id,
            StudentTutorLink.is_active == True,
        )
        student_res = await session.execute(student_stmt)
        tutor_ids = list(student_res.scalars().all())
        if tutor_id not in tutor_ids:
            tutor_ids.append(tutor_id)
        reply_kb = build_student_menu(tutor_ids, message.from_user.username, message.from_user.id)

        await state.clear()
        
        # Send registration completion message and show the persistent student reply keyboard
        await message.answer(
            f"🎉 <b>Регистрация успешно завершена!</b>\n\n"
            f"👤 Имя: <b>{full_name}</b>\n"
            f"📞 Телефон: <b>{phone}</b>\n\n"
            f"Теперь вы зарегистрированы в системе <b>AcademicLink</b> и будете получать уведомления.\n\n"
            f"Используйте кнопки меню ниже, чтобы записаться на занятие или посмотреть свои записи.",
            parse_mode="HTML",
            reply_markup=reply_kb
        )


@router.message(F.text.func(lambda t: t and "записаться" in t.lower()))
async def cmd_book_select_tutor(message: Message, state: FSMContext) -> None:
    await state.clear()
    tg_id = message.from_user.id
    
    async with async_session_factory() as session:
        # Check all tutors this student is registered with
        student_stmt = select(Student).where(Student.telegram_id == tg_id).options(
            selectinload(Student.tutor_links).selectinload(StudentTutorLink.tutor)
        )
        student_res = await session.execute(student_stmt)
        student = student_res.scalar_one_or_none()
        
        if not student:
            await message.answer("⚠️ Вы не зарегистрированы как ученик в системе.")
            return

        active_links = [link for link in student.tutor_links if link.is_active and link.tutor]
        if not active_links:
            await message.answer("⚠️ Вы не зарегистрированы как ученик в системе.")
            return

        # Always send the first tutor's WebApp link. Inside the WebApp, they can switch via dropdown.
        from app.core.config import settings
        from urllib.parse import urlencode
        tutor = active_links[0].tutor
        params = {}
        if message.from_user.username:
            params["tg_username"] = message.from_user.username
        params["tg_id"] = str(tg_id)
        query_str = f"?{urlencode(params)}" if params else ""
        web_app_url = f"{settings.web_url}/book/{tutor.id}{query_str}"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📅 Записаться", web_app=WebAppInfo(url=web_app_url))
        ]])
        
        # Build new student reply menu to refresh their keyboard immediately
        tutor_ids = [link.tutor_id for link in active_links]
        reply_kb = build_student_menu(tutor_ids, message.from_user.username, message.from_user.id)
        
        await message.answer(
            "Ваше меню обновлено. Используйте кнопку ниже или обновленную кнопку меню <b>«📅 Записаться»</b>:",
            parse_mode="HTML",
            reply_markup=reply_kb
        )
        await message.answer("Перейти к расписанию:", reply_markup=kb)


@router.message(F.text.func(lambda t: t and "мои записи" in t.lower()))
async def cmd_my_bookings(message: Message, state: FSMContext) -> None:
    await state.clear()
    tg_id = message.from_user.id
    
    async with async_session_factory() as session:
        # Check if they are a registered student (could be registered under multiple tutors)
        student_stmt = select(Student.id).where(Student.telegram_id == tg_id)
        student_res = await session.execute(student_stmt)
        student_ids = student_res.scalars().all()
        
        if not student_ids:
            await message.answer("⚠️ Вы не зарегистрированы как ученик в системе.")
            return
            
        # Get active bookings (PENDING, CONFIRMED) scheduled in the future or recent
        now_utc = datetime.now(timezone.utc)
        bookings_stmt = (
            select(Booking)
            .where(
                Booking.student_id.in_(student_ids),
                Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
                Booking.appointment_time >= now_utc
            )
            .options(selectinload(Booking.tutor))
            .order_by(Booking.appointment_time.asc())
        )
        bookings_res = await session.execute(bookings_stmt)
        bookings = bookings_res.scalars().all()
        
        if not bookings:
            text = (
                "🗂 <b>Ваши записи</b>\n\n"
                "У вас пока нет предстоящих записей на занятия.\n\n"
                "Вы можете записаться на новое занятие с помощью кнопки «📅 Записаться» в меню!"
            )
            await message.answer(text, parse_mode="HTML")
            return
            
        # Format the list of bookings beautifully
        lines = ["🗂 <b>Ваши предстоящие занятия:</b>\n"]
        kb_rows = []
        for b in bookings:
            dt_str = fmt_date(b.appointment_time)
            time_str = fmt_time(b.appointment_time)
            status_emoji = STATUS_EMOJI.get(b.status.value, "🟡")
            status_text = "подтверждено" if b.status == BookingStatus.CONFIRMED else "ожидает подтверждения"
            tutor_name = b.tutor.name if b.tutor else "Преподаватель"
            
            lines.append(
                f"{status_emoji} <b>{dt_str} в {time_str}</b> (Репетитор: {tutor_name})\n"
                f"   Услуга: {b.service_type}\n"
                f"   Статус: <i>{status_text}</i>\n"
            )

            # Add cancel/reschedule buttons for future bookings
            appt_time = b.appointment_time
            if appt_time.tzinfo is None:
                appt_time = appt_time.replace(tzinfo=timezone.utc)
            else:
                appt_time = appt_time.astimezone(timezone.utc)

            if appt_time > now_utc:
                row = [
                    InlineKeyboardButton(
                        text=f"❌ Отменить ({time_str})",
                        callback_data=f"student_cancel_init:{b.id}",
                    ),
                    InlineKeyboardButton(
                        text=f"🔄 Перенести ({time_str})",
                        callback_data=f"student_reschedule_init:{b.id}",
                    ),
                ]
                kb_rows.append(row)
            
        text = "\n".join(lines)
        inline_kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
        await message.answer(text, parse_mode="HTML", reply_markup=inline_kb)


# ═════════════════════════════════════════════════════════════════════
#  📅 Расписание & 🟡 Новые заявки — paginated lists
# ═════════════════════════════════════════════════════════════════════


async def _build_bookings_page(
    tg_id: int, 
    page: int, 
    statuses: list[BookingStatus],
    title: str,
    callback_prefix: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """
    Query bookings by status and build text + keyboard for the given page.
    """
    async with async_session_factory() as session:
        tutor = await _get_tutor(tg_id, session)
        if tutor is None:
            return _NOT_REGISTERED, None

        now_utc = datetime.now(timezone.utc)
        result = await session.execute(
            select(Booking)
            .where(
                Booking.tutor_id == tutor.id,
                Booking.status.in_(statuses),
                Booking.appointment_time >= now_utc,
            )
            .options(selectinload(Booking.student))
            .order_by(Booking.appointment_time)
        )
        bookings = result.scalars().all()

    if not bookings:
        return (
            f"{title}\n\n"
            "Сейчас записей в этом списке нет.\n\n"
            "<i>Нажмите «🏠 Главная» для возврата.</i>"
        ), None

    total = len(bookings)
    total_pages = math.ceil(total / PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_bookings = bookings[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]

    lines = [
        f"{title}  ({total} записей)",
    ]

    # Group by date → time slots
    by_date: dict[str, list[Booking]] = defaultdict(list)
    for b in page_bookings:
        date_key = fmt_date_dot(b.appointment_time)
        by_date[date_key].append(b)

    for date_label, date_bookings in by_date.items():
        lines.append(f"\n📅 <b>{date_label}</b>\n")

        for b in date_bookings:
            icon = STATUS_EMOJI.get(b.status.value, "❓")
            name = b.student.full_name if b.student else "—"
            lines.append(
                f"{icon} 🕒 <b>{fmt_time(b.appointment_time)}</b> — "
                f"{name} ({b.service_type})"
            )

    # Build keyboard: action buttons per booking + pagination
    kb_rows = [build_booking_actions(b) for b in page_bookings]
    if total_pages > 1:
        kb_rows.append(build_page_nav(page, total_pages, prefix=callback_prefix))

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb_rows)


@router.message(F.text == "📅 Расписание")
async def cmd_schedule(message: Message, state: FSMContext) -> None:
    await state.clear()
    tg_id = message.from_user.id
    async with async_session_factory() as session:
        tutor = await _get_tutor(tg_id, session)
        if tutor is None:
            await _handle_non_tutor(message, session)
            return

    text, kb = await _build_bookings_page(
        tg_id, 0, [BookingStatus.CONFIRMED], "📅 <b>Расписание</b>", "page_sch"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb or MAIN_MENU)


@router.message(F.text == "🟡 Новые заявки")
async def cmd_new_requests(message: Message, state: FSMContext) -> None:
    await state.clear()
    tg_id = message.from_user.id
    async with async_session_factory() as session:
        tutor = await _get_tutor(tg_id, session)
        if tutor is None:
            await _handle_non_tutor(message, session)
            return

    text, kb = await _build_bookings_page(
        tg_id, 0, [BookingStatus.PENDING], "🟡 <b>Новые заявки</b>", "page_new"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb or MAIN_MENU)


@router.callback_query(F.data.startswith("page_sch:"))
async def cb_page_sch(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[1])
    text, kb = await _build_bookings_page(
        callback.from_user.id, page, [BookingStatus.CONFIRMED], "📅 <b>Расписание</b>", "page_sch"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("page_new:"))
async def cb_page_new(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[1])
    text, kb = await _build_bookings_page(
        callback.from_user.id, page, [BookingStatus.PENDING], "🟡 <b>Новые заявки</b>", "page_new"
    )
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


# ═════════════════════════════════════════════════════════════════════
#  👥 Ученики — FSM student search
# ═════════════════════════════════════════════════════════════════════


@router.message(F.text == "👥 Ученики")
async def cmd_students(message: Message, state: FSMContext) -> None:
    """Show a distinct list of students (deduplicated by phone)."""
    await state.clear()
    tg_id = message.from_user.id

    async with async_session_factory() as session:
        tutor = await _get_tutor(tg_id, session)
        if tutor is None:
            await _handle_non_tutor(message, session)
            return

        # Distinct students belonging to this tutor
        result = await session.execute(
            select(Student)
            .join(StudentTutorLink)
            .where(
                StudentTutorLink.tutor_id == tutor.id,
                StudentTutorLink.is_active == True,
            )
            .order_by(Student.full_name)
        )
        students = result.scalars().all()

    if not students:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Найти по номеру", callback_data="student_search_init")]
        ])
        await message.answer(
            "👥 <b>Ученики</b>\n\n"
            "У вас пока нет активных учеников.\n"
            "Они появятся здесь после первой записи.\n\n"
            "<i>Вы также можете найти и восстановить архивированного ученика по его номеру телефона.</i>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    lines = [f"👥 <b>Ученики</b>  ({len(students)})\n"]
    for s in students:
        lines.append(f"👤 <b>{s.full_name}</b>")
        lines.append(f"     📞 {s.phone}\n")

    # Build inline buttons: View History + Contact per student
    kb_rows = [
        [InlineKeyboardButton(text="🔍 Найти по номеру телефона", callback_data="student_search_init")]
    ]
    for s in students:
        row = [
            InlineKeyboardButton(
                text=f"📋 {s.full_name}",
                callback_data=f"student_history:{s.id}",
            ),
        ]
        if s.telegram_username:
            clean = s.telegram_username.lstrip("@")
            row.append(InlineKeyboardButton(
                text="💬",
                url=f"https://t.me/{clean}",
            ))
        kb_rows.append(row)

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


@router.callback_query(F.data == "student_search_init")
async def cb_student_search_init(callback: CallbackQuery, state: FSMContext) -> None:
    """Prompt the tutor for the student's phone number."""
    await state.set_state(StudentSearch.waiting_phone)
    await callback.message.answer(
        "🔍 <b>Поиск ученика</b>\n\n"
        "Введите номер телефона ученика в международном формате.\n"
        "<i>Например: +79001234567</i>",
        parse_mode="HTML",
        reply_markup=BACK_KB,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("student_history:"))
async def cb_student_history(callback: CallbackQuery) -> None:
    """Show booking history for a specific student."""
    student_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Ошибка доступа.", show_alert=True)
            return

        result = await session.execute(
            select(Student, StudentTutorLink)
            .join(StudentTutorLink)
            .where(
                Student.id == student_id,
                StudentTutorLink.tutor_id == tutor.id,
            )
            .options(selectinload(Student.bookings))
        )
        row = result.first()

    if row is None:
        await callback.answer("Ученик не найден.", show_alert=True)
        return

    student, link = row

    bookings = sorted([b for b in student.bookings if b.tutor_id == tutor.id], key=lambda b: b.appointment_time, reverse=True)

    active_label = "" if link.is_active else " (архивирован)"
    lines = [
        f"👤 <b>{student.full_name}</b>{active_label}",
        f"{fmt_contact_links(student.phone, student.telegram_username)}",
        f"Всего занятий: <b>{len(bookings)}</b>",
    ]

    if not bookings:
        lines.append("\n<i>История занятий пуста.</i>")
    else:
        lines.append("\n<b>История:</b>\n")
        for b in bookings[:10]:
            icon = STATUS_EMOJI.get(b.status.value, "❓")
            lines.append(
                f"{icon} 🕒 {fmt_full(b.appointment_time)} — {b.service_type}"
            )
        if len(bookings) > 10:
            lines.append(f"\n<i>… и ещё {len(bookings) - 10}</i>")

    # Build inline contact button + book lesson + delete/restore button
    kb_rows = []
    contact_row = []
    if student.telegram_username:
        clean = student.telegram_username.lstrip("@")
        contact_row.append(InlineKeyboardButton(
            text="💬 Написать",
            url=f"https://t.me/{clean}",
        ))
    if contact_row:
        kb_rows.append(contact_row)

    if link.is_active:
        kb_rows.append([
            InlineKeyboardButton(
                text="➕ Записать на урок",
                callback_data=f"manual_book_init:{student.id}",
            )
        ])
        kb_rows.append([
            InlineKeyboardButton(
                text="🗑 Удалить ученика",
                callback_data=f"student_delete_init:{student.id}",
            )
        ])
    else:
        kb_rows.append([
            InlineKeyboardButton(
                text="🟢 Восстановить ученика",
                callback_data=f"student_restore:{student.id}",
            )
        ])

    await callback.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()


@router.message(StudentSearch.waiting_phone)
async def process_student_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()
    digits = "".join(c for c in phone if c.isdigit())
    
    if len(digits) == 11 and (digits.startswith("89") or digits.startswith("79")):
        normalized_phone = "+79" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        normalized_phone = "+79" + digits[1:]
    else:
        await message.answer(
            "Введите корректный номер мобильного телефона РФ (например, +79109215428).\n"
            "<i>Например: +79109215428</i>",
            parse_mode="HTML",
            reply_markup=BACK_KB,
        )
        return

    await state.clear()
    await _show_student_card(message, normalized_phone)


@router.message(Command("student"))
async def cmd_student_direct(message: Message, state: FSMContext) -> None:
    """Direct /student +79... command (bypasses FSM)."""
    await state.clear()
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor is None:
            await _handle_non_tutor(message, session)
            return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "<b>Использование:</b> <code>/student +79001234567</code>",
            parse_mode="HTML",
        )
        return
    
    raw_phone = parts[1].strip()
    digits = "".join(c for c in raw_phone if c.isdigit())
    if len(digits) == 11 and (digits.startswith("89") or digits.startswith("79")):
        normalized_phone = "+79" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        normalized_phone = "+79" + digits[1:]
    else:
        normalized_phone = raw_phone  # fallback

    await _show_student_card(message, normalized_phone)


async def _show_student_card(message: Message, phone: str) -> None:
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor is None:
            await _handle_non_tutor(message, session)
            return

        result = await session.execute(
            select(Student, StudentTutorLink)
            .join(StudentTutorLink)
            .where(
                Student.phone == phone,
                StudentTutorLink.tutor_id == tutor.id,
            )
            .options(selectinload(Student.bookings))
        )
        row = result.first()

    if row is None:
        await message.answer(
            f"Ученик с номером <code>{phone}</code> не найден.\n\n"
            "<i>Проверьте номер и попробуйте ещё раз.</i>",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    student, link = row

    bookings = sorted([b for b in student.bookings if b.tutor_id == tutor.id], key=lambda b: b.appointment_time, reverse=True)

    active_label = "" if link.is_active else " (архивирован)"
    lines = [
        f"👤 <b>{student.full_name}</b>{active_label}",
        f"{fmt_contact_links(student.phone, student.telegram_username)}",
        f"Всего занятий: <b>{len(bookings)}</b>",
    ]

    if not bookings:
        lines.append("\n<i>История занятий пуста.</i>")
    else:
        lines.append("\n<b>История:</b>\n")
        for b in bookings[:10]:
            icon = STATUS_EMOJI.get(b.status.value, "❓")
            lines.append(
                f"{icon} 🕒 {fmt_full(b.appointment_time)} — {b.service_type}"
            )
        if len(bookings) > 10:
            lines.append(f"\n<i>… и ещё {len(bookings) - 10}</i>")

    # Build inline contact button + book lesson + delete/restore button
    kb_rows = []
    contact_row = []
    if student.telegram_username:
        clean = student.telegram_username.lstrip("@")
        contact_row.append(InlineKeyboardButton(
            text="💬 Написать",
            url=f"https://t.me/{clean}",
        ))
    if contact_row:
        kb_rows.append(contact_row)

    if link.is_active:
        kb_rows.append([
            InlineKeyboardButton(
                text="➕ Записать на урок",
                callback_data=f"manual_book_init:{student.id}",
            )
        ])
        kb_rows.append([
            InlineKeyboardButton(
                text="🗑 Удалить ученика",
                callback_data=f"student_delete_init:{student.id}",
            )
        ])
    else:
        kb_rows.append([
            InlineKeyboardButton(
                text="🟢 Восстановить ученика",
                callback_data=f"student_restore:{student.id}",
            )
        ])

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


# ═════════════════════════════════════════════════════════════════════
# ⚙️ Настройки — profile + toggle
# ═════════════════════════════════════════════════════════════════════


def _settings_text(tutor: Tutor, slots: list[AvailabilitySlot]) -> str:
    days_map = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
    by_day: dict[int, list[AvailabilitySlot]] = {}
    for s in slots:
        by_day.setdefault(s.weekday, []).append(s)

    schedule_lines = []
    for day in sorted(by_day):
        windows = ", ".join(f"{s.start_time:%H:%M}–{s.end_time:%H:%M}" for s in by_day[day])
        schedule_lines.append(f"  {days_map[day]}: {windows}")
    
    schedule_text = "\n".join(schedule_lines) if schedule_lines else "<i>Слоты не настроены</i>"

    remind_icon = "🔔" if tutor.wants_reminders else "🔕"
    gcal_status = "🟢 Подключен" if tutor.google_token_json else "🔴 Не подключен"

    from app.core.bot import get_bot_username
    bot_username = get_bot_username()

    tg_invite_link = f"<code>https://t.me/{bot_username}?start=ref_{tutor.id}</code>"

    return (
        f"⚙️ <b>Настройки</b>\n\n"
        f"👤 <b>{tutor.name}</b>\n"
        f"📅 <b>Google Календарь:</b> {gcal_status}\n\n"
        f"🤖 <b>Ссылка для записи в Telegram:</b>\n{tg_invite_link}\n\n"
        f"⏰ <b>Рабочие часы:</b>\n"
        f"{schedule_text}\n\n"
        f"{remind_icon} <b>Напоминания:</b> {'Вкл' if tutor.wants_reminders else 'Откл'}\n"
    )


def _settings_kb(tutor: Tutor) -> InlineKeyboardMarkup:
    remind_text = "🔕 Уведомления" if tutor.wants_reminders else "🔔 Уведомления"
    
    from app.core.config import settings
    
    is_localhost = "localhost" in settings.web_url or "127.0.0.1" in settings.web_url
    
    gcal_btn = (
        InlineKeyboardButton(text="📅 Отключить Google", callback_data="gcal_disconnect")
        if tutor.google_token_json
        else (
            InlineKeyboardButton(text="📅 Подключить Google", callback_data="gcal_localhost_warning")
            if is_localhost
            else InlineKeyboardButton(text="📅 Подключить Google", url=f"{settings.web_url}/api/v1/auth/google/login/{tutor.id}")
        )
    )
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=remind_text, callback_data=f"toggle_remind:{tutor.id}"),
            InlineKeyboardButton(text="✍️ Изменить ФИО", callback_data="tutor_edit_name"),
        ],
        [
            InlineKeyboardButton(text="💎 Мои услуги", callback_data="manage_services"),
        ],
        [
            InlineKeyboardButton(text="⏰ Мои слоты", callback_data="manage_slots"),
            InlineKeyboardButton(text="💳 Реквизиты СБП", callback_data="manage_sbp"),
        ],
        [
            gcal_btn
        ],
    ])


@router.message(F.text == "⚙️ Настройки")
@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor is None:
            # Check if they are a student instead
            student_stmt = select(Student).where(Student.telegram_id == message.from_user.id).limit(1)
            student_res = await session.execute(student_stmt)
            linked_student = student_res.scalar_one_or_none()
            if linked_student:
                # Check active links
                student_stmt = select(StudentTutorLink.tutor_id).join(Student).where(
                    Student.telegram_id == message.from_user.id,
                    StudentTutorLink.is_active == True,
                )
                student_res = await session.execute(student_stmt)
                tutor_ids = list(student_res.scalars().all())
                if not tutor_ids:
                    await message.answer(
                        "❌ <b>Доступ ограничен</b>\n\n"
                        "Вы были удалены из базы учеников. Запись на новые занятия и просмотр расписания недоступны.\n"
                        "Пожалуйста, свяжитесь со своим преподавателем напрямую для восстановления доступа.",
                        parse_mode="HTML",
                        reply_markup=ReplyKeyboardRemove()
                    )
                    return

                # Show student settings (Notification Toggle and edit profile)
                remind_icon = "🔔" if linked_student.wants_reminders else "🔕"
                remind_label = "Вкл" if linked_student.wants_reminders else "Выкл"
                remind_kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"{remind_icon} Напоминания: {remind_label}",
                        callback_data="student_toggle_remind",
                    )],
                    [
                        InlineKeyboardButton(text="✍️ Изменить ФИО", callback_data="student_edit_name"),
                        InlineKeyboardButton(text="📱 Изменить телефон", callback_data="student_edit_phone"),
                    ]
                ])
                await message.answer(
                    f"⚙️ <b>Настройки ученика</b>\n\n"
                    f"Имя: <b>{linked_student.full_name}</b>\n"
                    f"Телефон: <code>{linked_student.phone}</code>\n\n"
                    f"Вы можете включить/отключить напоминания или обновить ваши контактные данные:",
                    parse_mode="HTML",
                    reply_markup=remind_kb,
                )
                return

            await message.answer(_NOT_REGISTERED, parse_mode="HTML")
            return

        result = await session.execute(
            select(AvailabilitySlot)
            .where(AvailabilitySlot.tutor_id == tutor.id)
            .order_by(AvailabilitySlot.weekday, AvailabilitySlot.start_time)
        )
        slots = result.scalars().all()

    await message.answer(
        _settings_text(tutor, slots), parse_mode="HTML", reply_markup=_settings_kb(tutor),
    )


# ── SBP Settings Handlers ───────────────────────────────────────────

def _sbp_settings_text(tutor: Tutor) -> str:
    phone = tutor.sbp_phone or "<i>не указан</i>"
    bank = tutor.sbp_bank or "<i>не указан</i>"
    
    return (
        f"💳 <b>Реквизиты СБП для переводов учеников</b>\n\n"
        f"Укажите ваши реквизиты, чтобы при записи ученики могли выбрать способ "
        f"оплаты «Перевод СБП» и увидеть ваши данные для перевода.\n\n"
        f"📱 <b>Телефон СБП:</b> {phone}\n"
        f"🏦 <b>Банк-получатель:</b> {bank}\n"
    )

def _sbp_settings_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Изменить телефон", callback_data="sbp_set_phone"),
            InlineKeyboardButton(text="🏦 Изменить банк", callback_data="sbp_set_bank"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад в настройки", callback_data="back_to_settings"),
        ]
    ])


@router.callback_query(F.data == "manage_sbp")
async def cb_manage_sbp(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Ошибка: вы не зарегистрированы.", show_alert=True)
            return
            
    await callback.message.edit_text(
        _sbp_settings_text(tutor),
        parse_mode="HTML",
        reply_markup=_sbp_settings_kb()
    )
    await callback.answer()


@router.callback_query(F.data == "sbp_set_phone")
async def cb_sbp_set_phone(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TutorSettingsStates.waiting_sbp_phone)
    await callback.message.edit_text(
        "📱 <b>Введите ваш номер телефона для переводов СБП:</b>\n\n"
        "Например: <code>+79991234567</code>\n\n"
        "<i>Для отмены пришлите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(TutorSettingsStates.waiting_sbp_phone)
async def process_sbp_phone(message: Message, state: FSMContext) -> None:
    if message.text == "/cancel":
        await state.clear()
        # Redirect back to settings page
        await _show_settings_after_input(message)
        return

    phone = message.text.strip()
    digits = "".join(c for c in phone if c.isdigit())
    
    # Normalize Russian mobile numbers to +79XXXXXXXXX (starts with 79, length 11)
    if len(digits) == 11 and (digits.startswith("89") or digits.startswith("79")):
        digits = "79" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        digits = "79" + digits[1:]

    if len(digits) != 11 or not digits.startswith("79"):
        await message.answer(
            "❌ Неверный формат номера телефона для СБП.\n"
            "Пожалуйста, введите корректный российский мобильный номер телефона (11 цифр, например: +79991234567 или 89991234567)."
        )
        return

    normalized_phone = f"+{digits}"

    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor:
            tutor.sbp_phone = normalized_phone
            await session.commit()
            
    await state.clear()
    await message.answer("✅ Номер телефона СБП успешно сохранен!")
    await _show_settings_after_input(message)


@router.callback_query(F.data == "sbp_set_bank")
async def cb_sbp_set_bank(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TutorSettingsStates.waiting_sbp_bank)
    await callback.message.edit_text(
        "🏦 <b>Введите название банка для перевода СБП:</b>\n\n"
        "Например: <code>Т-Банк (Тинькофф)</code> или <code>Сбербанк</code>\n\n"
        "<i>Для отмены пришлите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()

@router.message(TutorSettingsStates.waiting_sbp_bank)
async def process_sbp_bank(message: Message, state: FSMContext) -> None:
    if message.text == "/cancel":
        await state.clear()
        await _show_settings_after_input(message)
        return

    bank = message.text.strip()
    if len(bank) < 2 or len(bank) > 100:
        await message.answer("❌ Пожалуйста, введите корректное название банка (от 2 до 100 символов).")
        return

    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor:
            tutor.sbp_bank = bank
            await session.commit()
            
    await state.clear()
    await message.answer("✅ Банк для СБП успешно сохранен!")
    await _show_settings_after_input(message)


# SBP Link and QR handlers removed


async def _show_settings_after_input(message: Message) -> None:
    """Helper to display settings page after an FSM input is complete."""
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor is None:
            return
        result = await session.execute(
            select(AvailabilitySlot)
            .where(AvailabilitySlot.tutor_id == tutor.id)
            .order_by(AvailabilitySlot.weekday, AvailabilitySlot.start_time)
        )
        slots = result.scalars().all()
    await message.answer(
        _settings_text(tutor, slots), parse_mode="HTML", reply_markup=_settings_kb(tutor),
    )




# ═════════════════════════════════════════════════════════════════════
#  📅 Отсутствие — sick leave / vacation management
# ═════════════════════════════════════════════════════════════════════


async def _show_absence_manager(callback_or_message, tutor_id: int) -> None:
    async with async_session_factory() as session:
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(TutorAbsence)
            .where(
                TutorAbsence.tutor_id == tutor_id,
                TutorAbsence.end_time >= now
            )
            .order_by(TutorAbsence.start_time)
        )
        absences = result.scalars().all()

    lines = ["📅 <b>Моё отсутствие</b>\n"]
    kb_rows = []
    
    if not absences:
        lines.append("У вас нет запланированных периодов отсутствия.")
    else:
        # BUG #032 fix: move import out of loop
        from app.bot.formatting import fmt_date_short
        for a in absences:
            reason = f" ({a.reason})" if a.reason else ""
            lines.append(f"• {fmt_full(a.start_time)} — {fmt_full(a.end_time)}{reason}")
            
            btn_label = f"🗑 Удалить: {fmt_date_short(a.start_time)}"
            kb_rows.append([InlineKeyboardButton(text=btn_label, callback_data=f"del_absence_cb:{a.id}")])

    lines.append("\n<i>Добавьте период (болезнь, отпуск), чтобы временно закрыть запись.</i>")

    kb_rows.append([InlineKeyboardButton(text="➕ Добавить период", callback_data="add_absence_init")])
    kb_rows.append([InlineKeyboardButton(text="⚡️ Занять время (сегодня)", callback_data="quick_block_today")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    text = "\n".join(lines)
    if isinstance(callback_or_message, CallbackQuery):
        await callback_or_message.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback_or_message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(F.text == "📅 Отсутствие")
async def cmd_absence(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor is None:
            await message.answer(_NOT_REGISTERED, parse_mode="HTML")
            return

    await _show_absence_manager(message, tutor.id)


# ── Absence Management Callbacks ─────────────────────────────────────


@router.callback_query(F.data == "quick_block_today")
async def cb_quick_block_today(callback: CallbackQuery) -> None:
    """Quickly block the rest of the current day."""
    now_local = datetime.now(MSK)
    today_weekday = now_local.weekday()

    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        # BUG #022 fix: guard against None tutor
        if tutor is None:
            await callback.answer("Ошибка доступа.", show_alert=True)
            return

        # Find the latest slot end_time for today's weekday
        result = await session.execute(
            select(AvailabilitySlot)
            .where(
                AvailabilitySlot.tutor_id == tutor.id,
                AvailabilitySlot.weekday == today_weekday,
            )
            .order_by(AvailabilitySlot.end_time.desc())
            .limit(1)
        )
        last_slot = result.scalar_one_or_none()

        if last_slot is None:
            # Default to 23:59:59 MSK if no slots are defined today
            end_hour = 23
            end_minute = 59
            end_of_day = now_local.replace(
                hour=end_hour, minute=end_minute, second=59, microsecond=0
            )
        else:
            end_hour = last_slot.end_time.hour
            end_minute = last_slot.end_time.minute
            end_of_day = now_local.replace(
                hour=end_hour, minute=end_minute, second=0, microsecond=0,
            )

        if now_local >= end_of_day:
            await callback.answer("Рабочий день уже закончен.", show_alert=True)
            return

        absence = TutorAbsence(
            tutor_id=tutor.id,
            start_time=now_local.astimezone(timezone.utc),
            end_time=end_of_day.astimezone(timezone.utc),
            reason="Личные дела (быстрая блокировка)"
        )
        session.add(absence)

        # Cancel overlapping
        stmt = select(Booking).where(
            Booking.tutor_id == tutor.id,
            Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
            Booking.appointment_time >= now_local.astimezone(timezone.utc),
            Booking.appointment_time < end_of_day.astimezone(timezone.utc)
        ).options(selectinload(Booking.student))

        result = await session.execute(stmt)
        overlapping = result.scalars().all()

        from app.core.bot import get_bot
        bot = get_bot()
        from app.services.google_calendar_service import delete_calendar_event
        for b in overlapping:
            b.status = BookingStatus.CANCELLED
            
            # Delete Google Calendar event if it was synced
            try:
                await delete_calendar_event(session, b)
            except Exception as exc:
                logger.error("Failed to delete Google event for quick blocked booking #%d: %s", b.id, exc)

            if b.student and b.student.telegram_id and bot:
                try:
                    await bot.send_message(
                        chat_id=b.student.telegram_id,
                        text=f"🔴 <b>Занятие отменено</b>\n\nПреподаватель <b>{tutor.name}</b> занят сегодня до конца дня.\nВаша запись на {fmt_full(b.appointment_time)} отменена.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        await session.commit()

    end_time_str = f"{end_hour:02d}:{end_minute:02d}"
    await callback.message.answer(
        f"⚡️ <b>Время занято!</b>\n\nВы заблокировали запись на сегодня до {end_time_str}.\n"
        f"Отменено записей: <b>{len(overlapping)}</b>",
        parse_mode="HTML",
        reply_markup=MAIN_MENU
    )
    await _show_absence_manager(callback.message, tutor.id)
    await callback.answer()


@router.callback_query(F.data == "add_absence_init")
async def cb_add_absence_init(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TutorAbsenceStates.waiting_start)
    await callback.message.answer(
        "📅 <b>Введите начало отсутствия</b> (ДД.ММ.ГГГГ ЧЧ:ММ)\n"
        "<i>Например: 15.05.2026 09:00</i>",
        parse_mode="HTML",
        reply_markup=BACK_KB
    )
    await callback.answer()


@router.message(TutorAbsenceStates.waiting_start)
async def process_absence_start(message: Message, state: FSMContext) -> None:
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        dt_utc = dt.astimezone(timezone.utc)
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте ДД.ММ.ГГГГ ЧЧ:ММ")
        return

    await state.update_data(start_time=dt_utc.isoformat())
    await state.set_state(TutorAbsenceStates.waiting_end)
    await message.answer(
        f"✅ Начало: <b>{fmt_full(dt_utc)}</b>\n\n"
        "📅 <b>Введите окончание отсутствия</b> (ДД.ММ.ГГГГ ЧЧ:ММ)\n"
        "<i>Например: 16.05.2026 18:00</i>",
        parse_mode="HTML",
        reply_markup=BACK_KB
    )


@router.message(TutorAbsenceStates.waiting_end)
async def process_absence_end(message: Message, state: FSMContext) -> None:
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        dt_utc = dt.astimezone(timezone.utc)
    except ValueError:
        await message.answer("❌ Неверный формат. Используйте ДД.ММ.ГГГГ ЧЧ:ММ")
        return

    data = await state.get_data()
    start_time = datetime.fromisoformat(data["start_time"])

    if dt_utc <= start_time:
        await message.answer("❌ Окончание должно быть позже начала.")
        return

    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor is None:
            await state.clear()
            await message.answer(_NOT_REGISTERED, parse_mode="HTML")
            return

        absence = TutorAbsence(
            tutor_id=tutor.id,
            start_time=start_time,
            end_time=dt_utc,
            reason=None
        )
        session.add(absence)
        
        # ── Handle Overlapping Bookings ──────────────────────────────
        stmt = select(Booking).where(
            Booking.tutor_id == tutor.id,
            Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
            Booking.appointment_time >= start_time,
            Booking.appointment_time < dt_utc
        ).options(selectinload(Booking.student))
        
        result = await session.execute(stmt)
        overlapping = result.scalars().all()
        
        cancelled_count = len(overlapping)
        from app.core.bot import get_bot
        bot = get_bot()

        from app.services.google_calendar_service import delete_calendar_event
        for b in overlapping:
            b.status = BookingStatus.CANCELLED
            
            # Delete Google Calendar event if it was synced
            try:
                await delete_calendar_event(session, b)
            except Exception as exc:
                logger.error("Failed to delete Google event for absence cancelled booking #%d: %s", b.id, exc)
            
            # Notify student
            if b.student and b.student.telegram_id and bot:
                student_text = (
                    f"🔴 <b>Занятие отменено</b>\n\n"
                    f"К сожалению, преподаватель <b>{tutor.name}</b> будет отсутствовать с {fmt_full(start_time)} по {fmt_full(dt_utc)}.\n\n"
                    f"Ваша запись на <b>{fmt_full(b.appointment_time)}</b> отменена."
                )
                try:
                    await bot.send_message(chat_id=b.student.telegram_id, text=student_text, parse_mode="HTML")
                except Exception as exc:
                    logger.error("Failed to notify student %d: %s", b.student.telegram_id, exc)

        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ Период отсутствия добавлен. Отменено занятий: <b>{cancelled_count}</b>",
        parse_mode="HTML",
        reply_markup=MAIN_MENU
    )
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor:
            await _show_absence_manager(message, tutor.id)


@router.message(F.text.startswith("/del_absence_"))
async def cmd_del_absence(message: Message) -> None:
    absence_id = int(message.text.split("_")[-1])
    async with async_session_factory() as session:
        absence = await session.get(TutorAbsence, absence_id)
        if absence:
            tutor = await _get_tutor(message.from_user.id, session)
            if tutor is None or absence.tutor_id != tutor.id:
                await message.answer("❌ Ошибка доступа.")
                return
            
            await session.delete(absence)
            await session.commit()
            await message.answer("🗑 Период отсутствия удален. Теперь это время снова доступно для записи.")
            await _show_absence_manager(message, tutor.id)
        else:
            await message.answer("❌ Запись не найдена.")


@router.callback_query(F.data.startswith("del_absence_cb:"))
async def cb_del_absence(callback: CallbackQuery) -> None:
    absence_id = int(callback.data.split(":")[-1])
    async with async_session_factory() as session:
        absence = await session.get(TutorAbsence, absence_id)
        if absence:
            tutor = await _get_tutor(callback.from_user.id, session)
            if tutor is None or absence.tutor_id != tutor.id:
                await callback.answer("Ошибка доступа.", show_alert=True)
                return
            
            await session.delete(absence)
            await session.commit()
            await callback.answer("🗑 Период отсутствия удален.")
            await _show_absence_manager(callback, tutor.id)
        else:
            await callback.answer("Запись не найдена.", show_alert=True)


# ═════════════════════════════════════════════════════════════════════
#  /today — daily briefing
# ═════════════════════════════════════════════════════════════════════


@router.message(Command("today"))
async def cmd_today(message: Message, state: FSMContext) -> None:
    """Show today's schedule as a morning briefing."""
    await state.clear()
    tg_id = message.from_user.id
    now = datetime.now(MSK)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    async with async_session_factory() as session:
        tutor = await _get_tutor(tg_id, session)
        if tutor is None:
            await message.answer(_NOT_REGISTERED, parse_mode="HTML")
            return

        result = await session.execute(
            select(Booking)
            .where(
                Booking.tutor_id == tutor.id,
                Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
                Booking.appointment_time >= day_start.astimezone(timezone.utc),
                Booking.appointment_time < day_end.astimezone(timezone.utc),
            )
            .options(selectinload(Booking.student))
            .order_by(Booking.appointment_time)
        )
        bookings = result.scalars().all()

    greeting = _greeting()

    if not bookings:
        await message.answer(
            f"{greeting}!\n\n"
            f"📅 <b>{fmt_date(now)}</b>\n\n"
            "На сегодня занятий нет.\n"
            "Новые записи появятся автоматически.\n\n"
            "<i>Нажмите «📅 Расписание» для просмотра.</i>",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    pend = sum(1 for b in bookings if b.status == BookingStatus.PENDING)
    conf = sum(1 for b in bookings if b.status == BookingStatus.CONFIRMED)

    lines = [
        f"{greeting}!",
        f"\n📅 <b>{fmt_date(now)}</b>\n",
        f"Занятий: <b>{len(bookings)}</b>  (🟢 {conf} · 🟡 {pend})\n",
    ]

    for b in bookings:
        lines.append(fmt_booking_compact(b))

    lines.append("\n<i>Нажмите «📅 Расписание» для управления.</i>")

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=MAIN_MENU)



# ── Edit Settings Handlers ──────────────────────────────────────────

@router.callback_query(F.data == "tutor_edit_name")
async def cb_tutor_edit_name(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TutorSettingsStates.waiting_name)
    await callback.message.answer(
        "✍️ <b>Введите ваше новое ФИО:</b>\n\n"
        "<i>Используйте буквы и пробелы (минимум 2 слова).\n"
        "Для отмены пришлите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "student_edit_name")
async def cb_student_edit_name(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StudentSettingsStates.waiting_name)
    await callback.message.answer(
        "✍️ <b>Введите ваше новое ФИО:</b>\n\n"
        "<i>Используйте буквы и пробелы (минимум 2 слова).\n"
        "Для отмены пришлите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "student_edit_phone")
async def cb_student_edit_phone(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(StudentSettingsStates.waiting_phone)
    await callback.message.answer(
        "📱 <b>Введите ваш новый телефон (в международном формате):</b>\n\n"
        "Например: <code>+79991234567</code>\n\n"
        "<i>Для отмены пришлите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(TutorSettingsStates.waiting_name)
async def process_tutor_edit_name(message: Message, state: FSMContext) -> None:
    if message.text == "/cancel":
        await state.clear()
        await _show_settings_after_input(message)
        return

    name = message.text.strip()
    parts = name.split()
    if len(parts) < 2:
        await message.answer("❌ ФИО должно содержать как минимум фамилию и имя (2 слова). Попробуйте еще раз:")
        return

    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor:
            tutor.name = name
            await session.commit()
            
    await state.clear()
    await message.answer("✅ Ваше ФИО успешно обновлено!")
    await _show_settings_after_input(message)


@router.message(StudentSettingsStates.waiting_name)
async def process_student_edit_name(message: Message, state: FSMContext) -> None:
    if message.text == "/cancel":
        await state.clear()
        await cmd_settings(message, state)
        return

    name = message.text.strip()
    parts = name.split()
    if len(parts) < 2:
        await message.answer("❌ ФИО должно содержать как минимум фамилию и имя (2 слова). Попробуйте еще раз:")
        return

    async with async_session_factory() as session:
        student_stmt = select(Student).where(Student.telegram_id == message.from_user.id).limit(1)
        res = await session.execute(student_stmt)
        student = res.scalar_one_or_none()
        if student:
            student.full_name = name
            await session.commit()
            
    await state.clear()
    await message.answer("✅ Ваше ФИО успешно обновлено!")
    await cmd_settings(message, state)


@router.message(StudentSettingsStates.waiting_phone)
async def process_student_edit_phone(message: Message, state: FSMContext) -> None:
    if message.text == "/cancel":
        await state.clear()
        await cmd_settings(message, state)
        return

    phone_input = message.text.strip()
    digits = "".join(c for c in phone_input if c.isdigit())
    
    if len(digits) == 11 and (digits.startswith("89") or digits.startswith("79")):
        normalized_phone = "+79" + digits[2:]
    elif len(digits) == 10 and digits.startswith("9"):
        normalized_phone = "+79" + digits[1:]
    else:
        await message.answer(
            "❌ Номер телефона должен быть мобильным номером РФ (например, +79109215428). Попробуйте еще раз:"
        )
        return
    phone = normalized_phone

    async with async_session_factory() as session:
        stmt = select(Student).where(Student.phone == phone, Student.telegram_id != message.from_user.id)
        res = await session.execute(stmt)
        existing = res.scalar_one_or_none()
        if existing:
            await message.answer("❌ Этот номер телефона уже занят другим учеником. Введите другой номер:")
            return

        student_stmt = select(Student).where(Student.telegram_id == message.from_user.id).limit(1)
        res = await session.execute(student_stmt)
        student = res.scalar_one_or_none()
        if student:
            student.phone = phone
            await session.commit()
            
    await state.clear()
    await message.answer("✅ Ваш номер телефона успешно обновлен!")
    await cmd_settings(message, state)


# ═════════════════════════════════════════════════════════════════════
#  Callbacks
# ═════════════════════════════════════════════════════════════════════


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ── Confirm ──────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.split(":")[1])
    now = datetime.now(MSK)

    student_tg_id = None
    appt_text = ""
    service = ""

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        # BUG #001 fix: optimistic lock — only update if still PENDING
        if booking.status != BookingStatus.PENDING:
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        booking.status = BookingStatus.CONFIRMED

        # Sync to Google Calendar BEFORE commit (BUG #005 fix)
        from app.services.google_calendar_service import sync_booking_to_calendar
        try:
            await sync_booking_to_calendar(session, booking)
        except Exception as exc:
            logger.error("Failed to sync confirmed booking #%d to Google Calendar: %s", booking.id, exc)

        await session.commit()

        # Collect student info for notification
        if booking.student and booking.student.telegram_id:
            student_tg_id = booking.student.telegram_id
        appt_text = fmt_full(booking.appointment_time)
        service = booking.service_type

    await callback.message.edit_text(
        f"🟢 <b>Запись подтверждена</b>\n\n"
        f"🕒 {fmt_full(now)}",
        parse_mode="HTML",
    )
    await callback.answer("Подтверждено")
    logger.info("Booking #%d confirmed by tg_id=%d", booking_id, callback.from_user.id)

    # ── Notify student via Telegram ──────────────────────────────
    if student_tg_id:
        from app.core.bot import get_bot

        bot = get_bot()
        if bot:
            text = (
                f"🟢 <b>Ваше занятие подтверждено!</b>\n\n"
                f"🕒 {appt_text}\n"
                f"📚 {service}\n\n"
                f"<i>До встречи!</i>"
            )
            try:
                await bot.send_message(
                    chat_id=student_tg_id, text=text, parse_mode="HTML",
                )
                logger.info(
                    "Student tg_id=%d notified about confirmation of booking #%d",
                    student_tg_id, booking_id,
                )
            except Exception as exc:
                logger.error(
                    "Failed to notify student tg_id=%d: %s",
                    student_tg_id, exc,
                )


# ── Cancel (Immediate with Inline Confirmation) ──────────────────────


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel_init(callback: CallbackQuery, state: FSMContext) -> None:
    """Ask for confirmation before cancelling."""
    booking_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        booking = await session.get(Booking, booking_id)
        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            await callback.message.edit_reply_markup(reply_markup=None)
            return

    # ── Cancellation safety buffer ─────────────────────────────────
    from app.core.config import settings
    now_utc = datetime.now(timezone.utc)
    # BUG #024 fix: ensure appointment_time is timezone-aware before subtraction
    appt_time = booking.appointment_time
    if appt_time.tzinfo is None:
        appt_time = appt_time.replace(tzinfo=timezone.utc)
    hours_until = (appt_time - now_utc).total_seconds() / 3600

    if hours_until > 0 and hours_until < settings.cancel_safety_hours:
        warning = (
            f"\n\n⚠️ <b>Это занятие начинается менее чем через "
            f"{settings.cancel_safety_hours} ч.!</b>\n"
            f"Ученик может не успеть увидеть уведомление."
        )
    else:
        warning = ""

    text = (
        "🔴 <b>Подтвердите отмену</b>\n\n"
        "Вы действительно хотите отменить эту запись?\n"
        "<i>Это действие нельзя отменить.</i>"
        f"{warning}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"cancel_confirm:{booking_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_abort"),
        ]
    ])
    
    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("cancel_confirm:"))
async def cb_cancel_confirm(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.split(":")[1])

    student_tg_id = None
    appt_text = ""
    service = ""

    tutor_name = "Преподаватель"
    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student), selectinload(Booking.tutor))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return

        # BUG #001 fix: optimistic lock check
        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            return

        booking.status = BookingStatus.CANCELLED

        # BUG #005 fix: Delete Google Calendar event BEFORE commit
        from app.services.google_calendar_service import delete_calendar_event
        try:
            await delete_calendar_event(session, booking)
        except Exception as exc:
            logger.error("Failed to delete Google event for cancelled booking #%d: %s", booking.id, exc)

        await session.commit()

        if booking.tutor:
            tutor_name = booking.tutor.name

        tg_username = booking.student.telegram_username if booking.student else None

        # Collect student info for notification
        if booking.student and booking.student.telegram_id:
            student_tg_id = booking.student.telegram_id
        appt_text = fmt_full(booking.appointment_time)
        service = booking.service_type

    # Build the "notify student" button
    kb_rows = []
    if tg_username:
        clean = tg_username.lstrip("@")
        kb_rows.append([InlineKeyboardButton(text="💬 Написать ученику", url=f"https://t.me/{clean}")])

    await callback.message.edit_text(
        "🔴 <b>Запись отменена</b>\n\n"
        "Вы можете написать ученику, чтобы объяснить причину.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    )
    await callback.answer("Запись отменена")
    logger.info("Booking #%d cancelled by tg_id=%d", booking_id, callback.from_user.id)

    # ── Notify student via Telegram ──────────────────────────────
    if student_tg_id:
        from app.core.bot import get_bot

        bot = get_bot()
        if bot:
            text = (
                f"🔴 <b>Ваше занятие отменено</b>\n\n"
                f"Преподаватель: <b>{tutor_name}</b>\n"
                f"🕒 {appt_text}\n"
                f"📚 {service}\n\n"
                f"<i>Свяжитесь с преподавателем для уточнения деталей.</i>"
            )
            try:
                await bot.send_message(
                    chat_id=student_tg_id, text=text, parse_mode="HTML",
                )
                logger.info(
                    "Student tg_id=%d notified about cancellation of booking #%d",
                    student_tg_id, booking_id,
                )
            except Exception as exc:
                logger.error(
                    "Failed to notify student tg_id=%d: %s",
                    student_tg_id, exc,
                )


@router.callback_query(F.data == "cancel_abort")
async def cb_cancel_abort(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отмена действия отклонена.")
    await callback.answer()


# ── Detail ───────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("detail:"))
async def cb_detail(callback: CallbackQuery) -> None:
    booking_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student))
        )
        booking = result.scalar_one_or_none()

    if booking is None:
        await callback.answer("Запись не найдена.", show_alert=True)
        return

    name = booking.student.full_name if booking.student else "—"
    phone = booking.student.phone if booking.student else "—"
    tg_user = booking.student.telegram_username if booking.student else None
    icon = STATUS_EMOJI.get(booking.status.value, "❓")
    label = STATUS_LABEL.get(booking.status.value, booking.status.value)

    # Build inline action buttons for detail view
    kb_rows = []
    if tg_user:
        clean = tg_user.lstrip("@")
        kb_rows.append([InlineKeyboardButton(
            text="💬 Написать", url=f"https://t.me/{clean}",
        )])

    # Reschedule button — only for active bookings
    if booking.status in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
        kb_rows.append([InlineKeyboardButton(
            text="🗓 Перенести",
            callback_data=f"reschedule_init:{booking.id}",
        )])

    detail_kb = InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None

    text = (
        f"🕒 <b>{fmt_time(booking.appointment_time)}</b> — "
        f"{fmt_date_dot(booking.appointment_time)}\n\n"
        f"👤 <b>{name}</b>\n"
        f"{fmt_contact_links(phone, tg_user)}\n\n"
        f"{booking.service_type}\n"
        f"{icon} {label}\n\n"
        f"<i>Создана: {fmt_full(booking.created_at)}</i>"
    )

    await callback.message.answer(text, parse_mode="HTML", reply_markup=detail_kb)
    await callback.answer()


# ── Toggle is_active ─────────────────────────────────────────────────


@router.callback_query(F.data.startswith("toggle:"))
async def cb_toggle(callback: CallbackQuery) -> None:
    tutor_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor is None:
            await callback.answer("Репетитор не найден.", show_alert=True)
            return
        if tutor.tg_id != callback.from_user.id:
            await callback.answer("Вы можете изменять только свой профиль.", show_alert=True)
            return

        tutor.is_active = not tutor.is_active
        await session.commit()
        alert = "🟢 Приём записей возобновлён." if tutor.is_active else "🔴 Приём записей приостановлен."

        result = await session.execute(
            select(AvailabilitySlot)
            .where(AvailabilitySlot.tutor_id == tutor.id)
            .order_by(AvailabilitySlot.weekday, AvailabilitySlot.start_time)
        )
        slots = result.scalars().all()

    await callback.message.edit_text(
        _settings_text(tutor, slots), parse_mode="HTML", reply_markup=_settings_kb(tutor),
    )
    await callback.answer(alert)
    logger.info("Tutor #%d toggled is_active=%s", tutor_id, tutor.is_active)


# ── Google Calendar Disconnect ────────────────────────────────────────


@router.callback_query(F.data == "gcal_disconnect")
async def cb_gcal_disconnect(callback: CallbackQuery) -> None:
    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Репетитор не найден.", show_alert=True)
            return

        tutor.google_token_json = None
        await session.commit()

        result = await session.execute(
            select(AvailabilitySlot)
            .where(AvailabilitySlot.tutor_id == tutor.id)
            .order_by(AvailabilitySlot.weekday, AvailabilitySlot.start_time)
        )
        slots = result.scalars().all()

    await callback.message.edit_text(
        _settings_text(tutor, slots), parse_mode="HTML", reply_markup=_settings_kb(tutor),
    )
    await callback.answer("📅 Google Календарь отключен.")


@router.callback_query(F.data == "gcal_localhost_warning")
async def cb_gcal_localhost_warning(callback: CallbackQuery) -> None:
    await callback.answer(
        "⚠️ Режим разработки\n\n"
        "Telegram не поддерживает ссылки 'localhost' в кнопках.\n\n"
        "Чтобы протестировать подключение Google Календаря, используйте ngrok и настройте публичный WEB_URL в файле .env.",
        show_alert=True,
    )


# ── Student Deletion (Archive) ───────────────────────────────────────


@router.callback_query(F.data.startswith("student_delete_init:"))
async def cb_student_delete_init(callback: CallbackQuery, state: FSMContext) -> None:
    student_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        student = await session.get(Student, student_id)
        if student is None:
            await callback.answer("Ученик не найден.", show_alert=True)
            return

    await state.set_state(StudentManagement.confirm_delete)
    await state.update_data(delete_student_id=student_id)

    text = (
        f"⚠️ <b>Удаление ученика</b>\n\n"
        f"Вы уверены, что хотите удалить ученика <b>{student.full_name}</b>?\n\n"
        f"<i>История его занятий сохранится в базе, но он больше "
        f"не будет отображаться в списках активных учеников.</i>"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data="student_delete_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="student_delete_abort"),
        ]
    ])

    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(StudentManagement.confirm_delete, F.data == "student_delete_confirm")
async def cb_student_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    student_id = data.get("delete_student_id")
    await state.clear()

    if not student_id:
        await callback.answer("Ошибка: ID ученика не найден.", show_alert=True)
        return

    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Ошибка доступа.", show_alert=True)
            return

        student = await session.get(Student, student_id)
        if student is None:
            await callback.answer("Ученик не найден.", show_alert=True)
            return

        # Soft delete inside the link table
        stmt = select(StudentTutorLink).where(
            StudentTutorLink.student_id == student_id,
            StudentTutorLink.tutor_id == tutor.id
        )
        res = await session.execute(stmt)
        link = res.scalar_one_or_none()
        if link:
            link.is_active = False

        # Cleanup: Cancel PENDING bookings for this tutor
        result = await session.execute(
            select(Booking).where(
                Booking.student_id == student_id,
                Booking.tutor_id == tutor.id,
                Booking.status == BookingStatus.PENDING,
            )
        )
        pending_bookings = result.scalars().all()
        for b in pending_bookings:
            b.status = BookingStatus.CANCELLED

        await session.commit()
        student_name = student.full_name

    await callback.message.edit_text(
        f"✅ Ученик <b>{student_name}</b> успешно удален (архивирован).",
        parse_mode="HTML",
    )
    await callback.answer("Ученик удален")
    logger.info("Student #%d archived by tg_id=%d", student_id, callback.from_user.id)


@router.callback_query(StudentManagement.confirm_delete, F.data == "student_delete_abort")
async def cb_student_delete_abort(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Удаление отменено.")
    await callback.answer()


@router.callback_query(F.data.startswith("student_restore:"))
async def cb_student_restore(callback: CallbackQuery) -> None:
    """Reactivate a soft-deleted (archived) student-tutor relationship."""
    student_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Ошибка доступа.", show_alert=True)
            return

        student = await session.get(Student, student_id)
        if student is None:
            await callback.answer("Ученик не найден.", show_alert=True)
            return

        stmt = select(StudentTutorLink).where(
            StudentTutorLink.student_id == student_id,
            StudentTutorLink.tutor_id == tutor.id
        )
        res = await session.execute(stmt)
        link = res.scalar_one_or_none()
        
        if link:
            link.is_active = True
            await session.commit()
            student_name = student.full_name
        else:
            await callback.answer("Связь с учеником не найдена.", show_alert=True)
            return

    await callback.message.edit_text(
        f"🟢 Ученик <b>{student_name}</b> успешно восстановлен в базе.",
        parse_mode="HTML",
    )
    await callback.answer("Ученик восстановлен")
    logger.info("Student #%d restored by tg_id=%d", student_id, callback.from_user.id)


# ═════════════════════════════════════════════════════════════════════
#  ⏰ Slot Management — manage availability slots via bot
# ═════════════════════════════════════════════════════════════════════

_DAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
_DAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


async def _show_slot_manager(callback_or_message, tutor_id: int) -> None:
    """Build and send the slot management view with current slots and weekday buttons."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(AvailabilitySlot)
            .where(AvailabilitySlot.tutor_id == tutor_id)
            .order_by(AvailabilitySlot.weekday, AvailabilitySlot.start_time)
        )
        slots = result.scalars().all()

    by_day: dict[int, list[AvailabilitySlot]] = {}
    for s in slots:
        by_day.setdefault(s.weekday, []).append(s)

    lines = ["⏰ <b>Управление слотами</b>\n"]
    if not by_day:
        lines.append("<i>Нет настроенных слотов.</i>\n")
    else:
        for day in sorted(by_day):
            windows = ", ".join(
                f"{s.start_time:%H:%M}–{s.end_time:%H:%M}" for s in by_day[day]
            )
            lines.append(f"  {_DAYS_SHORT[day]}: {windows}")
        lines.append("")

    lines.append("<i>Выберите день для настройки или очистки:</i>")

    # Build weekday buttons (2 per row)
    kb_rows = []
    for i in range(0, 7, 2):
        row = [InlineKeyboardButton(text=_DAYS_SHORT[i], callback_data=f"slot_day:{i}")]
        if i + 1 < 7:
            row.append(InlineKeyboardButton(text=_DAYS_SHORT[i + 1], callback_data=f"slot_day:{i + 1}"))
        kb_rows.append(row)

    # Clear buttons
    kb_rows.append([
        InlineKeyboardButton(text="🗑 Очистить все", callback_data="slot_clear_all"),
    ])
    kb_rows.append([
        InlineKeyboardButton(text="◀️ Назад в настройки", callback_data="back_to_settings"),
    ])

    text = "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if isinstance(callback_or_message, CallbackQuery):
        try:
            await callback_or_message.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await callback_or_message.message.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback_or_message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "manage_slots")
async def cb_manage_slots(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Не зарегистрированы.", show_alert=True)
            return
    await _show_slot_manager(callback, tutor.id)
    await callback.answer()


@router.callback_query(F.data == "back_to_settings")
async def cb_back_to_settings(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Не зарегистрированы.", show_alert=True)
            return
        result = await session.execute(
            select(AvailabilitySlot)
            .where(AvailabilitySlot.tutor_id == tutor.id)
            .order_by(AvailabilitySlot.weekday, AvailabilitySlot.start_time)
        )
        slots = result.scalars().all()
    try:
        await callback.message.edit_text(
            _settings_text(tutor, slots), parse_mode="HTML", reply_markup=_settings_kb(tutor),
        )
    except Exception:
        await callback.message.answer(
            _settings_text(tutor, slots), parse_mode="HTML", reply_markup=_settings_kb(tutor),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("slot_day:"))
async def cb_slot_day(callback: CallbackQuery, state: FSMContext) -> None:
    """User selected a weekday — show current slots for that day and ask for time range."""
    weekday = int(callback.data.split(":")[1])
    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Не зарегистрированы.", show_alert=True)
            return

        result = await session.execute(
            select(AvailabilitySlot).where(
                AvailabilitySlot.tutor_id == tutor.id,
                AvailabilitySlot.weekday == weekday,
            )
        )
        existing = result.scalars().all()

    current = ""
    if existing:
        windows = ", ".join(f"{s.start_time:%H:%M}–{s.end_time:%H:%M}" for s in existing)
        current = f"\n\n📌 Текущие: {windows}"

    await state.set_state(SlotManagement.entering_times)
    await state.update_data(slot_weekday=weekday, tutor_id=tutor.id)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Очистить этот день", callback_data=f"slot_clear_day:{weekday}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="manage_slots")],
    ])

    await callback.message.edit_text(
        f"⏰ <b>{_DAYS_FULL[weekday]}</b>{current}\n\n"
        f"Введите время работы в формате <b>ЧЧ:ММ-ЧЧ:ММ</b>\n"
        f"<i>Например: 09:00-18:00</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.message(SlotManagement.entering_times)
async def process_slot_times(message: Message, state: FSMContext) -> None:
    """Parse time range and create/replace AvailabilitySlot."""
    import re
    from datetime import time as dt_time

    text = message.text.strip()

    # Handle "Назад" button press
    if text == "◀️ Назад":
        await state.clear()
        await _send_dashboard(message)
        return

    # Parse HH:MM-HH:MM
    match = re.match(r"^(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})$", text)
    if not match:
        await message.answer(
            "❌ Неверный формат. Используйте <b>ЧЧ:ММ-ЧЧ:ММ</b>\n"
            "<i>Например: 09:00-18:00</i>",
            parse_mode="HTML",
        )
        return

    h1, m1, h2, m2 = int(match[1]), int(match[2]), int(match[3]), int(match[4])

    try:
        start = dt_time(h1, m1)
        end = dt_time(h2, m2)
    except ValueError:
        await message.answer("❌ Некорректное время. Проверьте часы и минуты.")
        return

    if start >= end:
        await message.answer("❌ Время начала должно быть раньше времени окончания.")
        return

    data = await state.get_data()
    weekday = data["slot_weekday"]
    tutor_id = data["tutor_id"]

    async with async_session_factory() as session:
        # Delete existing slots for this day
        result = await session.execute(
            select(AvailabilitySlot).where(
                AvailabilitySlot.tutor_id == tutor_id,
                AvailabilitySlot.weekday == weekday,
            )
        )
        for old_slot in result.scalars().all():
            await session.delete(old_slot)

        # Create new slot
        new_slot = AvailabilitySlot(
            tutor_id=tutor_id,
            weekday=weekday,
            start_time=start,
            end_time=end,
        )
        session.add(new_slot)
        await session.commit()

    await state.clear()
    await message.answer(
        f"✅ <b>{_DAYS_FULL[weekday]}</b>: {start:%H:%M}–{end:%H:%M}\n\n"
        f"Слот успешно обновлён!",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )
    logger.info("Slot updated: tutor=%d weekday=%d %s-%s", tutor_id, weekday, start, end)


@router.callback_query(F.data.startswith("slot_clear_day:"))
async def cb_slot_clear_day(callback: CallbackQuery, state: FSMContext) -> None:
    """Clear all slots for a specific weekday."""
    await state.clear()
    weekday = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Не зарегистрированы.", show_alert=True)
            return

        result = await session.execute(
            select(AvailabilitySlot).where(
                AvailabilitySlot.tutor_id == tutor.id,
                AvailabilitySlot.weekday == weekday,
            )
        )
        deleted = 0
        for slot in result.scalars().all():
            await session.delete(slot)
            deleted += 1
        await session.commit()

    if deleted == 0:
        await callback.answer(f"На {_DAYS_FULL[weekday]} слотов нет.", show_alert=True)
    else:
        await callback.answer(f"✅ Слоты на {_DAYS_FULL[weekday]} очищены.")
    await _show_slot_manager(callback, tutor.id)


@router.callback_query(F.data == "slot_clear_all")
async def cb_slot_clear_all(callback: CallbackQuery, state: FSMContext) -> None:
    """Clear ALL slots for the tutor."""
    await state.clear()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, очистить всё", callback_data="slot_clear_all_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="manage_slots"),
        ]
    ])
    await callback.message.edit_text(
        "⚠️ <b>Очистить все слоты?</b>\n\n"
        "Это удалит ваше расписание на все дни.\n"
        "<i>Новые записи будут невозможны, пока вы не добавите слоты.</i>",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "slot_clear_all_confirm")
async def cb_slot_clear_all_confirm(callback: CallbackQuery) -> None:
    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Не зарегистрированы.", show_alert=True)
            return

        result = await session.execute(
            select(AvailabilitySlot).where(AvailabilitySlot.tutor_id == tutor.id)
        )
        for slot in result.scalars().all():
            await session.delete(slot)
        await session.commit()

    await callback.answer("✅ Все слоты очищены.")
    await _show_slot_manager(callback, tutor.id)


# ═════════════════════════════════════════════════════════════════════
#  🔔 Toggle Reminders
# ═════════════════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("toggle_remind:"))
async def cb_toggle_remind(callback: CallbackQuery) -> None:
    tutor_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor is None:
            await callback.answer("Репетитор не найден.", show_alert=True)
            return
        if tutor.tg_id != callback.from_user.id:
            await callback.answer("Вы можете изменять только свой профиль.", show_alert=True)
            return

        tutor.wants_reminders = not tutor.wants_reminders
        await session.commit()
        alert = "🔔 Напоминания включены." if tutor.wants_reminders else "🔕 Напоминания отключены."

        result = await session.execute(
            select(AvailabilitySlot)
            .where(AvailabilitySlot.tutor_id == tutor.id)
            .order_by(AvailabilitySlot.weekday, AvailabilitySlot.start_time)
        )
        slots = result.scalars().all()

    await callback.message.edit_text(
        _settings_text(tutor, slots), parse_mode="HTML", reply_markup=_settings_kb(tutor),
    )
    await callback.answer(alert)
    logger.info("Tutor #%d toggled wants_reminders=%s", tutor_id, tutor.wants_reminders)


# Meeting link handlers removed


# ═════════════════════════════════════════════════════════════════════
#  💎 Service Management
# ═════════════════════════════════════════════════════════════════════


async def _show_service_manager(callback_or_message, tutor_id: int) -> None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Service).where(Service.tutor_id == tutor_id, Service.is_active == True)
        )
        services = result.scalars().all()

    lines = ["💎 <b>Мои услуги</b>\n"]
    kb_rows = []
    
    if not services:
        lines.append("<i>Список услуг пуст. Добавьте первую услугу.</i>")
    else:
        lines.append("Выберите услугу для настройки её длительности, стоимости и перерыва:\n")
        for s in services:
            price_text = f" — {s.price} руб." if s.price else " — бесплатно"
            lines.append(f"• <b>{s.name}</b> ({s.duration} мин){price_text}")
            kb_rows.append([InlineKeyboardButton(text=f"⚙️ {s.name}", callback_data=f"edit_service_select:{s.id}")])

    kb_rows.append([InlineKeyboardButton(text="➕ Добавить услугу", callback_data="add_service_init")])
    kb_rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")])

    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    text = "\n".join(lines)
    if isinstance(callback_or_message, CallbackQuery):
        await callback_or_message.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback_or_message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "manage_services")
async def cb_manage_services(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        # BUG #011 fix: guard against None tutor
        if tutor is None:
            await callback.answer("Ошибка доступа.", show_alert=True)
            return
    await _show_service_manager(callback, tutor.id)
    await callback.answer()


@router.callback_query(F.data == "add_service_init")
async def cb_add_service_init(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ServiceManagement.waiting_name)
    await callback.message.answer(
        "📝 <b>Название услуги</b>\n\n"
        "Например: <i>Подготовка к ЕГЭ</i> или <i>Бесплатный пробный урок</i>",
        parse_mode="HTML",
        reply_markup=BACK_KB
    )
    await callback.answer()


@router.message(ServiceManagement.waiting_name)
async def process_service_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    await state.update_data(srv_name=name)
    await state.set_state(ServiceManagement.waiting_duration)
    await message.answer(
        f"✅ Название: <b>{name}</b>\n\n"
        "📐 <b>Длительность урока (мин)</b>\n"
        "Например: 60",
        parse_mode="HTML",
        reply_markup=BACK_KB
    )


@router.message(ServiceManagement.waiting_duration)
async def process_service_duration(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit():
        await message.answer("❌ Введите число.")
        return
    
    duration = int(message.text)
    await state.update_data(srv_duration=duration)
    await state.set_state(ServiceManagement.waiting_buffer)
    await message.answer(
        f"✅ Длительность: <b>{duration} мин.</b>\n\n"
        "⏳ <b>Перерыв после урока (мин)</b>\n"
        "Например: 15 (если не нужен — 0)",
        parse_mode="HTML",
        reply_markup=BACK_KB
    )


@router.message(ServiceManagement.waiting_buffer)
async def process_service_buffer(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit():
        await message.answer("❌ Введите число.")
        return
    
    buffer = int(message.text)
    await state.update_data(srv_buffer=buffer)
    await state.set_state(ServiceManagement.waiting_price)
    await message.answer(
        f"✅ Перерыв: <b>{buffer} мин.</b>\n\n"
        "💰 <b>Стоимость (руб)</b>\n"
        "Например: 2000 (если бесплатно — 0)",
        parse_mode="HTML",
        reply_markup=BACK_KB
    )


@router.message(ServiceManagement.waiting_price)
async def process_service_price(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit():
        await message.answer("❌ Введите число.")
        return
    
    price = int(message.text)
    data = await state.get_data()
    
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        service = Service(
            tutor_id=tutor.id,
            name=data['srv_name'],
            duration=data['srv_duration'],
            buffer_time=data['srv_buffer'],
            price=price if price > 0 else None
        )
        session.add(service)
        await session.commit()
    
    await state.clear()
    await message.answer("✅ Услуга успешно добавлена!", reply_markup=MAIN_MENU)
    await _show_service_manager(message, tutor.id)


@router.message(F.text.startswith("/del_service_"))
async def cmd_del_service(message: Message) -> None:
    srv_id = int(message.text.split("_")[-1])
    async with async_session_factory() as session:
        service = await session.get(Service, srv_id)
        if service:
            tutor = await _get_tutor(message.from_user.id, session)
            # BUG #012 fix: guard against None tutor
            if tutor is None:
                await message.answer("❗ Ошибка доступа.")
                return
            if service.tutor_id == tutor.id:
                service.is_active = False # Soft delete
                await session.commit()
                await message.answer(f"🗑 Услуга «{service.name}» удалена.")
                await _show_service_manager(message, tutor.id)


@router.callback_query(F.data.startswith("del_service_cb:"))
async def cb_del_service(callback: CallbackQuery) -> None:
    srv_id = int(callback.data.split(":")[-1])
    async with async_session_factory() as session:
        service = await session.get(Service, srv_id)
        if service:
            tutor = await _get_tutor(callback.from_user.id, session)
            if service.tutor_id == tutor.id:
                service.is_active = False # Soft delete
                await session.commit()
                await callback.answer(f"🗑 Услуга «{service.name}» удалена.")
                await _show_service_manager(callback, tutor.id)
            else:
                await callback.answer("Ошибка доступа.", show_alert=True)
        else:
            await callback.answer("Услуга не найдена.", show_alert=True)


# ── Service Editing ──────────────────────────────────────────────────

async def _show_service_detail(callback_or_message, service_id: int, state: FSMContext = None) -> None:
    if state:
        await state.clear()
        
    async with async_session_factory() as session:
        service = await session.get(Service, service_id)
        if service is None or not service.is_active:
            if isinstance(callback_or_message, CallbackQuery):
                await callback_or_message.answer("Услуга не найдена.", show_alert=True)
            return

    price_text = f"{service.price} руб." if service.price else "бесплатно"
    buffer_text = f"{service.buffer_time} мин." if service.buffer_time else "нет"
    
    text = (
        f"💎 <b>Управление услугой</b>\n\n"
        f"📝 <b>Название:</b> {service.name}\n"
        f"📐 <b>Длительность:</b> {service.duration} мин.\n"
        f"⏳ <b>Перерыв после урока:</b> {buffer_text}\n"
        f"💰 <b>Стоимость:</b> {price_text}\n\n"
        f"Выберите параметр для редактирования:"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Изменить название", callback_data=f"edit_srv_field:name:{service.id}"),
        ],
        [
            InlineKeyboardButton(text="📐 Изменить длительность", callback_data=f"edit_srv_field:duration:{service.id}"),
        ],
        [
            InlineKeyboardButton(text="⏳ Изменить перерыв", callback_data=f"edit_srv_field:buffer:{service.id}"),
        ],
        [
            InlineKeyboardButton(text="💰 Изменить стоимость", callback_data=f"edit_srv_field:price:{service.id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить услугу", callback_data=f"del_service_cb:{service.id}"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад к списку", callback_data="manage_services"),
        ]
    ])
    
    if isinstance(callback_or_message, CallbackQuery):
        await callback_or_message.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await callback_or_message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("edit_service_select:"))
async def cb_edit_service_select(callback: CallbackQuery, state: FSMContext) -> None:
    service_id = int(callback.data.split(":")[-1])
    await _show_service_detail(callback, service_id, state)
    await callback.answer()


@router.callback_query(F.data.startswith("edit_srv_field:"))
async def cb_edit_srv_field(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    field = parts[1]
    service_id = int(parts[2])
    
    await state.update_data(editing_service_id=service_id)
    
    if field == "name":
        await state.set_state(ServiceManagement.editing_name)
        await callback.message.answer(
            "📝 <b>Новое название услуги</b>\n\n"
            "Введите новое название для этой услуги:",
            parse_mode="HTML",
            reply_markup=BACK_KB
        )
    elif field == "duration":
        await state.set_state(ServiceManagement.editing_duration)
        await callback.message.answer(
            "📐 <b>Новая длительность (мин)</b>\n\n"
            "Введите новую длительность урока в минутах:",
            parse_mode="HTML",
            reply_markup=BACK_KB
        )
    elif field == "buffer":
        await state.set_state(ServiceManagement.editing_buffer)
        await callback.message.answer(
            "⏳ <b>Новый перерыв (мин)</b>\n\n"
            "Введите новое время перерыва после урока в минутах:",
            parse_mode="HTML",
            reply_markup=BACK_KB
        )
    elif field == "price":
        await state.set_state(ServiceManagement.editing_price)
        await callback.message.answer(
            "💰 <b>Новая стоимость (руб)</b>\n\n"
            "Введите новую стоимость урока (если бесплатно — 0):",
            parse_mode="HTML",
            reply_markup=BACK_KB
        )
    await callback.answer()


@router.message(ServiceManagement.editing_name)
async def process_edit_service_name(message: Message, state: FSMContext) -> None:
    new_name = message.text.strip()
    data = await state.get_data()
    service_id = data["editing_service_id"]
    
    async with async_session_factory() as session:
        service = await session.get(Service, service_id)
        if service:
            service.name = new_name
            await session.commit()
            
    await state.clear()
    await message.answer("✅ Название услуги успешно изменено!", reply_markup=MAIN_MENU)
    await _show_service_detail(message, service_id)


@router.message(ServiceManagement.editing_duration)
async def process_edit_service_duration(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit():
        await message.answer("❌ Введите число.")
        return
        
    new_duration = int(message.text)
    data = await state.get_data()
    service_id = data["editing_service_id"]
    
    async with async_session_factory() as session:
        service = await session.get(Service, service_id)
        if service:
            service.duration = new_duration
            await session.commit()
            
    await state.clear()
    await message.answer("✅ Длительность услуги успешно изменена!", reply_markup=MAIN_MENU)
    await _show_service_detail(message, service_id)


@router.message(ServiceManagement.editing_buffer)
async def process_edit_service_buffer(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit():
        await message.answer("❌ Введите число.")
        return
        
    new_buffer = int(message.text)
    data = await state.get_data()
    service_id = data["editing_service_id"]
    
    async with async_session_factory() as session:
        service = await session.get(Service, service_id)
        if service:
            service.buffer_time = new_buffer
            await session.commit()
            
    await state.clear()
    await message.answer("✅ Перерыв после урока успешно изменен!", reply_markup=MAIN_MENU)
    await _show_service_detail(message, service_id)


@router.message(ServiceManagement.editing_price)
async def process_edit_service_price(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit():
        await message.answer("❌ Введите число.")
        return
        
    new_price = int(message.text)
    data = await state.get_data()
    service_id = data["editing_service_id"]
    
    async with async_session_factory() as session:
        service = await session.get(Service, service_id)
        if service:
            service.price = new_price if new_price > 0 else None
            await session.commit()
            
    await state.clear()
    await message.answer("✅ Стоимость услуги успешно изменена!", reply_markup=MAIN_MENU)
    await _show_service_detail(message, service_id)


# ═════════════════════════════════════════════════════════════════════
#  🗓 Перенос занятия (Reschedule)
# ═════════════════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("reschedule_init:"))
async def cb_reschedule_init(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the reschedule flow — open WebApp calendar."""
    booking_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        booking = await session.get(Booking, booking_id)
        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            await callback.answer("Эту запись нельзя перенести.", show_alert=True)
            return
        tutor_id = booking.tutor_id

    from app.core.config import settings
    web_app_url = f"{settings.web_url}/book/{tutor_id}?reschedule_booking_id={booking_id}&tutor_mode=true"

    await callback.message.answer(
        "🗓 <b>Перенос занятия</b>\n\n"
        "Нажмите на кнопку ниже, чтобы выбрать новое время занятия в календаре.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 Выбрать время", web_app=WebAppInfo(url=web_app_url))]
        ])
    )
    await callback.answer()


@router.message(RescheduleStates.waiting_datetime)
async def process_reschedule_datetime(message: Message, state: FSMContext) -> None:
    """Parse new date/time and execute the reschedule."""
    from app.services.booking_service import reschedule_booking

    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        dt_utc = dt.astimezone(timezone.utc)
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Используйте <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    booking_id = data["reschedule_booking_id"]

    async with async_session_factory() as session:
        try:
            booking, old_time = await reschedule_booking(
                session,
                booking_id=booking_id,
                new_appointment_time=dt_utc,
            )
        except ValueError as exc:
            await message.answer(
                f"❌ {exc}\n\n<i>Попробуйте другое время или нажмите «◀️ Назад».</i>",
                parse_mode="HTML",
            )
            return

        # Sync with Google Calendar
        from app.services.google_calendar_service import sync_booking_to_calendar
        try:
            await sync_booking_to_calendar(session, booking)
        except Exception as exc:
            logger.error("Failed to sync rescheduled booking #%d to Calendar: %s", booking.id, exc)

        student_tg_id = None
        if booking.student and booking.student.telegram_id:
            student_tg_id = booking.student.telegram_id
        service_name = booking.service_type

    await state.clear()
    await message.answer(
        f"✅ <b>Занятие перенесено!</b>\n\n"
        f"🕒 {fmt_full(old_time)} → <b>{fmt_full(dt_utc)}</b>",
        parse_mode="HTML",
        reply_markup=MAIN_MENU,
    )
    logger.info("Booking #%d rescheduled by tg_id=%d", booking_id, message.from_user.id)

    # ── Notify student ────────────────────────────────────────────────
    if student_tg_id:
        from app.core.bot import get_bot
        bot = get_bot()
        if bot:
            text = (
                f"🔄 <b>Ваше занятие перенесено</b>\n\n"
                f"📚 {service_name}\n"
                f"🕒 Было: {fmt_full(old_time)}\n"
                f"🕒 Стало: <b>{fmt_full(dt_utc)}</b>\n\n"
                f"<i>Если у вас есть вопросы, свяжитесь с репетитором.</i>"
            )
            try:
                await bot.send_message(chat_id=student_tg_id, text=text, parse_mode="HTML")
                logger.info("Student tg_id=%d notified about reschedule of booking #%d", student_tg_id, booking_id)
            except Exception as exc:
                logger.error("Failed to notify student tg_id=%d: %s", student_tg_id, exc)


# ── Tutor Reschedule Approval ────────────────────────────────────────

@router.callback_query(F.data.startswith("tr_a:"))
async def cb_tutor_resched_approve(callback: CallbackQuery) -> None:
    """Tutor approves the student's reschedule request."""
    from app.services.booking_service import (
        check_availability,
        check_double_booking,
        check_tutor_absence,
    )
    from app.services.google_calendar_service import sync_booking_to_calendar

    parts = callback.data.split(":")
    booking_id = int(parts[1])
    new_time_ts = int(parts[2])
    new_time = datetime.fromtimestamp(new_time_ts, tz=timezone.utc)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student), selectinload(Booking.tutor))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await callback.answer("❌ Запись не найдена.", show_alert=True)
            return

        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            await callback.answer("❌ Эту запись больше нельзя перенести.", show_alert=True)
            return

        # Double check availability/conflicts at execution time
        from app.db.models import Service
        service = await session.get(Service, booking.service_id) if booking.service_id else None
        duration = service.duration if service else 60
        buffer = service.buffer_time if service else 0

        try:
            await check_availability(session, tutor_id=booking.tutor_id, appointment_time=new_time)
            await check_tutor_absence(session, tutor_id=booking.tutor_id, appointment_time=new_time)
            await check_double_booking(
                session,
                tutor_id=booking.tutor_id,
                appointment_time=new_time,
                lesson_duration=duration,
                buffer_time=buffer,
                exclude_booking_id=booking.id,
            )
        except ValueError as exc:
            await callback.answer(f"❌ Невозможно перенести: {exc}", show_alert=True)
            return

        old_time = booking.appointment_time
        booking.appointment_time = new_time
        booking.status = BookingStatus.CONFIRMED
        await session.commit()

        # Sync with Google Calendar
        try:
            await sync_booking_to_calendar(session, booking)
        except Exception as exc:
            logger.error("Failed to sync rescheduled booking #%d to Calendar: %s", booking.id, exc)

        student_tg_id = booking.student.telegram_id if booking.student else None
        student_name = booking.student.full_name if booking.student else "Ученик"
        service_name = booking.service_type

    # Edit the tutor's notification message to show success
    await callback.message.edit_text(
        f"✅ <b>Перенос подтверждён!</b>\n\n"
        f"Ученик: <b>{student_name}</b>\n"
        f"Занятие: {service_name}\n"
        f"Было: {fmt_full(old_time)}\n"
        f"Стало: <b>{fmt_full(new_time)}</b>",
        parse_mode="HTML"
    )
    await callback.answer("Перенос занятия подтверждён!")

    # Notify student
    if student_tg_id:
        from app.core.bot import get_bot
        bot = get_bot()
        if bot:
            text = (
                f"✅ <b>Преподаватель подтвердил перенос занятия!</b>\n\n"
                f"Услуга: {service_name}\n"
                f"Было: {fmt_full(old_time)}\n"
                f"Новое время: <b>{fmt_full(new_time)}</b>\n\n"
                f"<i>Занятие успешно обновлено в расписании.</i>"
            )
            try:
                await bot.send_message(chat_id=student_tg_id, text=text, parse_mode="HTML")
            except Exception as exc:
                logger.error("Failed to notify student tg_id=%d of reschedule confirmation: %s", student_tg_id, exc)


# ── Tutor Reschedule Rejection ───────────────────────────────────────

@router.callback_query(F.data.startswith("tr_r:"))
async def cb_tutor_resched_reject(callback: CallbackQuery) -> None:
    """Tutor rejects the student's reschedule request."""
    parts = callback.data.split(":")
    booking_id = int(parts[1])
    proposed_time_ts = int(parts[2])
    proposed_time = datetime.fromtimestamp(proposed_time_ts, tz=timezone.utc)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await callback.answer("❌ Запись не найдена.", show_alert=True)
            return

        student_tg_id = booking.student.telegram_id if booking.student else None
        student_name = booking.student.full_name if booking.student else "Ученик"
        student_username = booking.student.telegram_username if booking.student else None
        service_name = booking.service_type
        current_time = booking.appointment_time

    # Edit the tutor's message to show rejection but keep the contact button
    if student_username:
        contact_url = f"https://t.me/{student_username}"
    else:
        contact_url = f"tg://user?id={student_tg_id}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💬 Связаться с учеником",
                url=contact_url
            )
        ]
    ])

    await callback.message.edit_text(
        f"❌ <b>Запрос на перенос отклонён!</b>\n\n"
        f"Ученик: <b>{student_name}</b>\n"
        f"Занятие: {service_name}\n"
        f"Текущее время (без изменений): {fmt_full(current_time)}\n"
        f"Отклоненный вариант: <s>{fmt_full(proposed_time)}</s>",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer("Запрос на перенос отклонён.")

    # Notify student
    if student_tg_id:
        from app.core.bot import get_bot
        bot = get_bot()
        if bot:
            text = (
                f"❌ <b>Преподаватель отклонил запрос на перенос занятия на {fmt_full(proposed_time)}.</b>\n\n"
                f"Услуга: {service_name}\n"
                f"Занятие остается в прежнее время: <b>{fmt_full(current_time)}</b>\n\n"
                f"<i>Пожалуйста, свяжитесь с преподавателем для уточнения деталей.</i>"
            )
            try:
                await bot.send_message(chat_id=student_tg_id, text=text, parse_mode="HTML")
            except Exception as exc:
                logger.error("Failed to notify student tg_id=%d of reschedule rejection: %s", student_tg_id, exc)


# ═════════════════════════════════════════════════════════════════════
#  ➕ Ручная запись (Manual Booking)
# ═════════════════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("manual_book_init:"))
async def cb_manual_book_init(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the manual booking flow — ask for date/time."""
    student_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)
        if tutor is None:
            await callback.answer("Не зарегистрированы.", show_alert=True)
            return

        student = await session.get(Student, student_id)
        if student is None:
            await callback.answer("Ученик не найден.", show_alert=True)
            return
        student_name = student.full_name

    await state.set_state(ManualBookingStates.waiting_datetime)
    await state.update_data(manual_book_student_id=student_id, manual_book_tutor_id=tutor.id)

    await callback.message.answer(
        f"➕ <b>Запись на урок</b>\n\n"
        f"👤 Ученик: <b>{student_name}</b>\n\n"
        f"Введите дату и время (<b>ДД.ММ.ГГГГ ЧЧ:ММ</b>)\n"
        f"<i>Например: 25.05.2026 14:00</i>",
        parse_mode="HTML",
        reply_markup=BACK_KB,
    )
    await callback.answer()


# ═════════════════════════════════════════════════════════════════════
#  🎓 Student: Cancel / Reschedule / Reminder Toggle
# ═════════════════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("student_cancel_init:"))
async def cb_student_cancel_init(callback: CallbackQuery) -> None:
    """Ask the student for cancellation confirmation, with a safety-buffer warning."""
    booking_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            return

        # Verify ownership
        if not booking.student or booking.student.telegram_id != callback.from_user.id:
            await callback.answer("Это не ваша запись.", show_alert=True)
            return

        # Check active link
        stmt = select(StudentTutorLink).where(
            StudentTutorLink.student_id == booking.student_id,
            StudentTutorLink.tutor_id == booking.tutor_id,
            StudentTutorLink.is_active == True
        )
        res = await session.execute(stmt)
        link = res.scalar_one_or_none()
        if not link:
            await callback.answer("❌ Ошибка доступа: вы были удалены из базы учеников этого преподавателя.", show_alert=True)
            return

    # Cancellation safety buffer
    from app.core.config import settings
    now_utc = datetime.now(timezone.utc)
    appt_time = booking.appointment_time
    if appt_time.tzinfo is None:
        appt_time = appt_time.replace(tzinfo=timezone.utc)
    else:
        appt_time = appt_time.astimezone(timezone.utc)
    hours_until = (appt_time - now_utc).total_seconds() / 3600

    warning = ""
    if 0 < hours_until < settings.cancel_safety_hours:
        warning = (
            f"\n\n⚠️ <b>Это занятие начинается менее чем через "
            f"{settings.cancel_safety_hours} ч.!</b>\n"
            f"Репетитор может не успеть увидеть уведомление."
        )

    text = (
        "🔴 <b>Отмена занятия</b>\n\n"
        f"🕒 {fmt_full(booking.appointment_time)}\n"
        f"📚 {booking.service_type}\n\n"
        "Вы действительно хотите отменить это занятие?\n"
        "<i>Это действие нельзя отменить.</i>"
        f"{warning}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"student_cancel_confirm:{booking_id}"),
            InlineKeyboardButton(text="❌ Нет", callback_data="student_cancel_abort"),
        ]
    ])

    await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("student_cancel_confirm:"))
async def cb_student_cancel_confirm(callback: CallbackQuery) -> None:
    """Execute student cancellation: set CANCELLED, delete calendar event, notify tutor."""
    booking_id = int(callback.data.split(":")[1])

    tutor_tg_id = None
    appt_text = ""
    service_name = ""
    student_name = ""

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student), selectinload(Booking.tutor))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            return

        # Verify ownership
        if not booking.student or booking.student.telegram_id != callback.from_user.id:
            await callback.answer("Это не ваша запись.", show_alert=True)
            return

        # Check active link
        stmt = select(StudentTutorLink).where(
            StudentTutorLink.student_id == booking.student_id,
            StudentTutorLink.tutor_id == booking.tutor_id,
            StudentTutorLink.is_active == True
        )
        res = await session.execute(stmt)
        link = res.scalar_one_or_none()
        if not link:
            await callback.answer("❌ Ошибка доступа: вы были удалены из базы учеников этого преподавателя.", show_alert=True)
            return

        booking.status = BookingStatus.CANCELLED

        # BUG #005 fix: Delete Google Calendar event BEFORE commit
        from app.services.google_calendar_service import delete_calendar_event
        try:
            await delete_calendar_event(session, booking)
        except Exception as exc:
            logger.error("Failed to delete Google event for student-cancelled booking #%d: %s", booking.id, exc)

        await session.commit()

        # Collect tutor info for notification
        if booking.tutor:
            tutor_tg_id = booking.tutor.tg_id
        appt_text = fmt_full(booking.appointment_time)
        service_name = booking.service_type
        student_name = booking.student.full_name if booking.student else "Ученик"

    await callback.message.edit_text(
        "🔴 <b>Занятие отменено</b>\n\n"
        f"🕒 {appt_text}\n"
        f"📚 {service_name}",
        parse_mode="HTML",
    )
    await callback.answer("Занятие отменено")
    logger.info("Booking #%d cancelled by student tg_id=%d", booking_id, callback.from_user.id)

    # ── Notify tutor via Telegram ──────────────────────────────
    if tutor_tg_id:
        from app.core.bot import get_bot
        bot = get_bot()
        if bot:
            text = (
                f"🔴 <b>Ученик отменил занятие</b>\n\n"
                f"👤 {student_name}\n"
                f"🕒 {appt_text}\n"
                f"📚 {service_name}\n\n"
                f"<i>Это время снова доступно для записи.</i>"
            )
            try:
                await bot.send_message(chat_id=tutor_tg_id, text=text, parse_mode="HTML")
                logger.info("Tutor tg_id=%d notified about student cancellation of booking #%d", tutor_tg_id, booking_id)
            except Exception as exc:
                logger.error("Failed to notify tutor tg_id=%d: %s", tutor_tg_id, exc)


@router.callback_query(F.data == "student_cancel_abort")
async def cb_student_cancel_abort(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Отмена занятия отклонена.")
    await callback.answer()


# ── Student Reschedule ───────────────────────────────────────────────


@router.callback_query(F.data.startswith("student_reschedule_init:"))
async def cb_student_reschedule_init(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the student rescheduling flow — open WebApp calendar."""
    booking_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            await callback.answer("Эту запись нельзя перенести.", show_alert=True)
            return
        # Verify ownership
        if not booking.student or booking.student.telegram_id != callback.from_user.id:
            await callback.answer("Это не ваша запись.", show_alert=True)
            return

        # Check active link
        stmt = select(StudentTutorLink).where(
            StudentTutorLink.student_id == booking.student_id,
            StudentTutorLink.tutor_id == booking.tutor_id,
            StudentTutorLink.is_active == True
        )
        res = await session.execute(stmt)
        link = res.scalar_one_or_none()
        if not link:
            await callback.answer("❌ Ошибка доступа: вы были удалены из базы учеников этого преподавателя.", show_alert=True)
            return
        tutor_id = booking.tutor_id

    from app.core.config import settings
    web_app_url = f"{settings.web_url}/book/{tutor_id}?reschedule_booking_id={booking_id}"

    await callback.message.answer(
        "🗓 <b>Перенос занятия</b>\n\n"
        f"Текущее время: <b>{fmt_full(booking.appointment_time)}</b>\n\n"
        "Нажмите на кнопку ниже, чтобы выбрать новое время занятия в календаре.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 Выбрать время", web_app=WebAppInfo(url=web_app_url))]
        ])
    )
    await callback.answer()


@router.message(StudentRescheduleStates.waiting_datetime)
async def process_student_reschedule_datetime(message: Message, state: FSMContext) -> None:
    """Parse new date/time and execute the student reschedule."""
    from app.services.booking_service import (
        check_availability,
        check_double_booking,
        check_tutor_absence,
    )
    from app.services.google_calendar_service import sync_booking_to_calendar

    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        dt_utc = dt.astimezone(timezone.utc)
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Используйте <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>",
            parse_mode="HTML",
        )
        return

    if dt_utc <= datetime.now(timezone.utc):
        await message.answer("❌ Нельзя перенести занятие в прошлое.")
        return

    data = await state.get_data()
    booking_id = data["student_reschedule_booking_id"]

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student), selectinload(Booking.tutor))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await message.answer("❌ Запись не найдена.")
            await state.clear()
            return

        # Check active link
        stmt = select(StudentTutorLink).where(
            StudentTutorLink.student_id == booking.student_id,
            StudentTutorLink.tutor_id == booking.tutor_id,
            StudentTutorLink.is_active == True
        )
        res = await session.execute(stmt)
        link = res.scalar_one_or_none()
        if not link:
            await message.answer("❌ Ошибка доступа: вы были удалены из базы учеников этого преподавателя.")
            await state.clear()
            return

        # Check if the new time is the same as the current time
        booking_time = booking.appointment_time
        if booking_time.tzinfo is None:
            booking_time = booking_time.replace(tzinfo=timezone.utc)
        if booking_time == dt_utc:
            await message.answer("❌ Вы выбрали то же самое время, что и установлено сейчас.")
            return

        # Fetch service for duration/buffer
        from app.db.models import Service
        service = await session.get(Service, booking.service_id) if booking.service_id else None
        duration = service.duration if service else 60
        buffer = service.buffer_time if service else 0

        try:
            await check_availability(session, tutor_id=booking.tutor_id, appointment_time=dt_utc)
            await check_tutor_absence(session, tutor_id=booking.tutor_id, appointment_time=dt_utc)
            await check_double_booking(
                session,
                tutor_id=booking.tutor_id,
                appointment_time=dt_utc,
                lesson_duration=duration,
                buffer_time=buffer,
                exclude_booking_id=booking.id,
            )
        except ValueError as exc:
            await message.answer(
                f"❌ {exc}\n\n<i>Попробуйте другое время.</i>",
                parse_mode="HTML",
            )
            return

        old_time = booking.appointment_time
        tutor_tg_id = booking.tutor.tg_id if booking.tutor else None
        service_name = booking.service_type
        student_name = booking.student.full_name if booking.student else "Ученик"
        student_username = booking.student.telegram_username if booking.student else None
        student_tg_id = booking.student.telegram_id if booking.student else None

    await state.clear()
    await message.answer(
        f"⏳ <b>Запрос на перенос отправлен преподавателю и ожидает подтверждения</b>\n\n"
        f"🕒 Текущее время: {fmt_full(old_time)}\n"
        f"🕒 Предложенное время: <b>{fmt_full(dt_utc)}</b>\n\n"
        f"<i>Мы пришлем вам уведомление, как только преподаватель подтвердит или отклонит запрос.</i>",
        parse_mode="HTML",
    )
    logger.info("Booking #%d reschedule requested by student tg_id=%d to %s", booking_id, message.from_user.id, dt_utc.isoformat())

    # ── Notify tutor ────────────────────────────────────────────────
    if tutor_tg_id:
        from app.core.bot import get_bot
        bot = get_bot()
        if bot:
            if student_username:
                contact_url = f"https://t.me/{student_username}"
            else:
                contact_url = f"tg://user?id={student_tg_id}"

            new_time_ts = int(dt_utc.timestamp())

            text = (
                f"📥 <b>Запрос на перенос занятия от ученика</b>\n\n"
                f"Ученик: <b>{student_name}</b>\n"
                f"Занятие: {service_name}\n"
                f"Было: {fmt_full(old_time)}\n"
                f"Предлагает перенести на: <b>{fmt_full(dt_utc)}</b>\n\n"
                f"Пожалуйста, подтвердите или отклоните перенос."
            )
            
            tutor_kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        # BUG #004 fix: shortened prefix to stay within 64-byte limit
                        callback_data=f"tr_a:{booking_id}:{new_time_ts}"
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить перенос",
                        callback_data=f"tr_r:{booking_id}:{new_time_ts}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="💬 Связаться с учеником",
                        url=contact_url
                    )
                ]
            ])

            try:
                await bot.send_message(
                    chat_id=tutor_tg_id,
                    text=text,
                    parse_mode="HTML",
                    reply_markup=tutor_kb
                )
                logger.info("Tutor tg_id=%d notified about reschedule request for booking #%d", tutor_tg_id, booking_id)
            except Exception as exc:
                logger.error("Failed to notify tutor tg_id=%d: %s", tutor_tg_id, exc)


# ── Student Reminder Toggle ─────────────────────────────────────────


@router.callback_query(F.data == "student_toggle_remind")
async def cb_student_toggle_remind(callback: CallbackQuery) -> None:
    """Toggle Student.wants_reminders and update the inline button."""
    tg_id = callback.from_user.id

    async with async_session_factory() as session:
        result = await session.execute(
            select(Student).where(Student.telegram_id == tg_id)
        )
        student = result.scalar_one_or_none()

        if student is None:
            await callback.answer("Ошибка: вы не зарегистрированы.", show_alert=True)
            return

        # Check if they have ANY active tutor links
        stmt = select(StudentTutorLink.tutor_id).where(
            StudentTutorLink.student_id == student.id,
            StudentTutorLink.is_active == True
        )
        res = await session.execute(stmt)
        if not res.scalars().all():
            await callback.answer("Доступ ограничен: у вас нет активных преподавателей.", show_alert=True)
            return

        student.wants_reminders = not student.wants_reminders
        await session.commit()
        new_state = student.wants_reminders

    remind_icon = "🔔" if new_state else "🔕"
    remind_label = "Вкл" if new_state else "Выкл"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{remind_icon} Напоминания: {remind_label}",
            callback_data="student_toggle_remind",
        )],
        [
            InlineKeyboardButton(text="✍️ Изменить ФИО", callback_data="student_edit_name"),
            InlineKeyboardButton(text="📱 Изменить телефон", callback_data="student_edit_phone"),
        ]
    ])

    try:
        await callback.message.edit_reply_markup(reply_markup=kb)
    except Exception:
        pass
    alert = "🔔 Напоминания включены." if new_state else "🔕 Напоминания отключены."
    await callback.answer(alert)
    logger.info("Student tg_id=%d toggled wants_reminders=%s", tg_id, new_state)


@router.message(ManualBookingStates.waiting_datetime)
async def process_manual_book_datetime(message: Message, state: FSMContext) -> None:
    """Parse date/time for manual booking, then ask for service selection."""
    try:
        dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
        dt_utc = dt.astimezone(timezone.utc)
    except ValueError:
        await message.answer("❌ Используйте формат: <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>", parse_mode="HTML")
        return

    data = await state.get_data()
    tutor_id = data["manual_book_tutor_id"]

    async with async_session_factory() as session:
        result = await session.execute(
            select(Service).where(Service.tutor_id == tutor_id, Service.is_active == True)
        )
        services = result.scalars().all()

    if not services:
        await message.answer("❌ У вас нет активных услуг. Сначала добавьте их в настройках.")
        await state.clear()
        return

    await state.update_data(manual_book_time=dt_utc.isoformat())
    await state.set_state(ManualBookingStates.waiting_service_type)

    buttons = []
    for s in services:
        buttons.append([InlineKeyboardButton(text=s.name, callback_data=f"manual_srv:{s.id}")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        f"✅ Дата: <b>{fmt_full(dt_utc)}</b>\n\n"
        f"📚 <b>Выберите услугу:</b>",
        parse_mode="HTML",
        reply_markup=kb
    )


@router.callback_query(ManualBookingStates.waiting_service_type, F.data.startswith("manual_srv:"))
async def cb_manual_book_service(callback: CallbackQuery, state: FSMContext) -> None:
    from app.services.booking_service import create_booking_internal

    service_id = int(callback.data.split(":")[1])
    data = await state.get_data()
    student_id = data["manual_book_student_id"]
    tutor_id = data["manual_book_tutor_id"]
    appointment_time = datetime.fromisoformat(data["manual_book_time"])

    async with async_session_factory() as session:
        try:
            booking = await create_booking_internal(
                session,
                student_id=student_id,
                tutor_id=tutor_id,
                service_id=service_id,
                appointment_time=appointment_time,
            )
        except ValueError as exc:
            await callback.message.answer(f"❌ {exc}")
            return

        student_name = booking.student.full_name if booking.student else "—"
        student_tg_id = booking.student.telegram_id if booking.student else None

    await state.clear()
    await callback.message.answer(
        f"✅ <b>Занятие создано!</b>\n\n"
        f"👤 {student_name}\n"
        f"🕒 {fmt_full(appointment_time)}\n"
        f"📚 {booking.service_type}\n"
        f"🟢 Подтверждено",
        parse_mode="HTML",
        reply_markup=MAIN_MENU
    )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()

    # ── Notify student ────────────────────────────────────────────────
    if student_tg_id:
        from app.core.bot import get_bot
        bot = get_bot()
        if bot:
            text = (
                f"🟢 <b>Репетитор записал вас на занятие</b>\n\n"
                f"🕒 {fmt_full(appointment_time)}\n"
                f"📚 {booking.service_type}\n\n"
                f"<i>До встречи!</i>"
            )
            try:
                await bot.send_message(chat_id=student_tg_id, text=text, parse_mode="HTML")
                logger.info("Student tg_id=%d notified about manual booking", student_tg_id)
            except Exception as exc:
                logger.error("Failed to notify student tg_id=%d: %s", student_tg_id, exc)


# ═════════════════════════════════════════════════════════════════════
#  📢 Рассылка — Mass mailing to students
# ═════════════════════════════════════════════════════════════════════


@router.message(F.text == "📢 Рассылка")
async def cmd_broadcast(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor is None:
            await message.answer(_NOT_REGISTERED, parse_mode="HTML")
            return

        # Count ALL reachable students (regardless of telegram_id)
        result = await session.execute(
            select(func.count(StudentTutorLink.student_id)).where(
                StudentTutorLink.tutor_id == tutor.id,
                StudentTutorLink.is_active == True,
            )
        )
        total_students = result.scalar_one()

    await state.set_state(BroadcastStates.composing)
    await state.update_data(tutor_id=tutor.id)

    await message.answer(
        f"📢 <b>Рассылка</b>\n\n"
        f"У вас <b>{total_students}</b> учеников.\n"
        f"<i>(Внимание: Бот сможет отправить сообщение автоматически только тем ученикам, которые хотя бы раз запускали этого бота. В остальных случаях бот выдаст ошибку доставки, и им нужно будет написать вручную).</i>\n\n"
        f"Введите текст сообщения для рассылки.",
        parse_mode="HTML",
        reply_markup=BACK_KB,
    )


@router.message(BroadcastStates.composing)
async def process_broadcast_text(message: Message, state: FSMContext) -> None:
    """Preview the broadcast message and ask for confirmation."""
    text = message.text
    if not text or text == "◀️ Назад":
        await state.clear()
        await _send_dashboard(message)
        return

    await state.update_data(broadcast_text=text)

    data = await state.get_data()
    tutor_id = data.get("tutor_id")

    tutor_name = "Преподаватель"
    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor:
            tutor_name = tutor.name

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="broadcast_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel"),
        ]
    ])

    preview_message = (
        f"📢 <b>Предпросмотр рассылки:</b>\n\n"
        f"📢 <b>Сообщение от преподавателя {tutor_name}</b>:\n\n"
        f"{text}\n\n"
        f"<i>К сообщению будет прикреплена кнопка:</i>\n"
        f"💬 <b>Написать репетитору</b>\n\n"
        f"Отправить это сообщение всем вашим ученикам?"
    )

    await message.answer(
        preview_message,
        parse_mode="HTML",
        reply_markup=kb,
    )


@router.callback_query(F.data == "broadcast_confirm")
async def cb_broadcast_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Execute the broadcast."""
    import asyncio

    data = await state.get_data()
    broadcast_text = data.get("broadcast_text", "")
    tutor_id = data.get("tutor_id")
    await state.clear()

    if not broadcast_text or not tutor_id:
        await callback.answer("Ошибка: данные рассылки утеряны.", show_alert=True)
        return

    # Fetch tutor details and ALL active students for this tutor
    tutor_name = "Преподаватель"
    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor:
            tutor_name = tutor.name

        result = await session.execute(
            select(Student)
            .join(StudentTutorLink)
            .where(
                StudentTutorLink.tutor_id == tutor_id,
                StudentTutorLink.is_active == True,
            )
        )
        students = result.scalars().all()

    if not students:
        await callback.message.edit_text(
            "📢 Нет доступных учеников для рассылки.",
            parse_mode="HTML",
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"📢 Отправка... (0/{len(students)})",
    )
    await callback.answer()

    from app.core.bot import get_bot
    bot = get_bot()
    if bot is None:
        await callback.message.edit_text("❌ Бот не инициализирован.")
        return

    success = 0
    failed = 0
    no_tg_id = 0
    
    formatted_message = f"📢 <b>Сообщение от преподавателя {tutor_name}</b>:\n\n{broadcast_text}"
    
    tutor_username = callback.from_user.username
    if tutor_username:
        contact_url = f"https://t.me/{tutor_username}"
    else:
        # Fallback to direct user ID link using telegram protocol
        contact_url = f"tg://user?id={tutor.tg_id}"

    for i, student in enumerate(students):
        if not student.telegram_id:
            no_tg_id += 1
            continue
            
        student_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Написать репетитору", url=contact_url)]
        ])

        try:
            await bot.send_message(
                chat_id=student.telegram_id,
                text=formatted_message,
                parse_mode="HTML",
                reply_markup=student_kb,
            )
            success += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "Broadcast to student tg_id=%d failed: %s",
                student.telegram_id, exc,
            )
        # Rate limiting
        if (i + 1) % 20 == 0:
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(0.05)

    # BUG #028 fix: show stats in the completion message, not just log
    if success > 0:
        await callback.message.edit_text(
            f"📢 <b>Рассылка успешно выполнена!</b>\n\n"
            f"✅ Отправлено: <b>{success}</b>\n"
            f"❌ Ошибки: <b>{failed}</b>\n"
            f"➖ Без Telegram: <b>{no_tg_id}</b>\n"
            f"📊 Всего учеников: <b>{len(students)}</b>",
            parse_mode="HTML",
        )
    else:
        await callback.message.edit_text(
            f"❌ <b>Не удалось отправить рассылку.</b>\n\n"
            f"❌ Ошибки: <b>{failed}</b>\n"
            f"➖ Без Telegram: <b>{no_tg_id}</b>\n"
            f"📊 Всего учеников: <b>{len(students)}</b>",
            parse_mode="HTML",
        )
        
    logger.info(
        "Broadcast by tutor_id=%d: success=%d failed=%d no_id=%d total=%d",
        tutor_id, success, failed, no_tg_id, len(students),
    )


@router.callback_query(F.data == "broadcast_cancel")
async def cb_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("📢 Рассылка отменена.")
    await callback.answer()


# ═════════════════════════════════════════════════════════════════════
#  💳 P2P SBP — Confirm / Reject Payment
# ═════════════════════════════════════════════════════════════════════

@router.callback_query(F.data.startswith("confirm_p2p:"))
async def cb_confirm_p2p(callback: CallbackQuery) -> None:
    """Репетитор подтверждает запись (СБП/наличные)."""
    booking_id = int(callback.data.split(":")[1])
    student_tg_id = None
    appt_text = ""
    service_name = ""
    payment_method = "transfer"

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if booking.status != BookingStatus.PENDING:
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        # BUG #003 fix: verify tutor ownership
        tutor_obj = await _get_tutor(callback.from_user.id, session)
        if tutor_obj is None or booking.tutor_id != tutor_obj.id:
            await callback.answer("Это не ваша запись.", show_alert=True)
            return

        booking.status = BookingStatus.CONFIRMED

        # BUG #005 fix: Sync to Google Calendar BEFORE commit
        from app.services.google_calendar_service import sync_booking_to_calendar
        try:
            await sync_booking_to_calendar(session, booking)
        except Exception as exc:
            logger.error("Failed to sync booking #%d to Calendar: %s", booking.id, exc)

        await session.commit()

        payment_method = booking.payment_method
        if booking.student and booking.student.telegram_id:
            student_tg_id = booking.student.telegram_id
        appt_text = fmt_full(booking.appointment_time)
        service_name = booking.service_type

    if payment_method == "cash":
        tutor_msg = (
            f"🟢 <b>Запись подтверждена и добавлена в расписание</b>\n\n"
            f"📚 {service_name}\n"
            f"🕒 {appt_text}"
        )
        tutor_toast = "Запись подтверждена ✅"
        student_msg = (
            f"🟢 <b>Ваша запись успешно подтверждена!</b>\n\n"
            f"🕒 {appt_text}\n"
            f"📚 {service_name}\n\n"
            f"<i>До встречи!</i>"
        )
    else:
        tutor_msg = (
            f"🟢 <b>Оплата подтверждена, запись утверждена</b>\n\n"
            f"📚 {service_name}\n"
            f"🕒 {appt_text}"
        )
        tutor_toast = "Оплата подтверждена ✅"
        student_msg = (
            f"🟢 <b>Ваша оплата подтверждена!</b>\n\n"
            f"🕒 {appt_text}\n"
            f"📚 {service_name}\n\n"
            f"<i>До встречи!</i>"
        )

    await callback.message.edit_text(tutor_msg, parse_mode="HTML")
    await callback.answer(tutor_toast)
    logger.info("Booking #%d confirmed by tutor tg_id=%d", booking_id, callback.from_user.id)

    # Notify student
    if student_tg_id:
        from app.core.bot import get_bot
        bot = get_bot()
        if bot:
            try:
                await bot.send_message(
                    chat_id=student_tg_id,
                    text=student_msg,
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.error("Failed to notify student tg_id=%d of confirm: %s", student_tg_id, exc)

@router.callback_query(F.data.startswith("cancel_p2p:"))
async def cb_cancel_p2p(callback: CallbackQuery) -> None:
    """Репетитор отклоняет запись (СБП/наличные)."""
    booking_id = int(callback.data.split(":")[1])
    student_tg_id = None
    appt_text = ""
    service_name = ""
    tg_username = None
    payment_method = "transfer"

    tutor_name = "Преподаватель"
    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(Booking.id == booking_id)
            .options(selectinload(Booking.student), selectinload(Booking.tutor))
        )
        booking = result.scalar_one_or_none()

        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if booking.status != BookingStatus.PENDING:
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        # BUG #003 fix: verify tutor ownership
        tutor_obj = await _get_tutor(callback.from_user.id, session)
        if tutor_obj is None or booking.tutor_id != tutor_obj.id:
            await callback.answer("Это не ваша запись.", show_alert=True)
            return

        booking.status = BookingStatus.CANCELLED
        await session.commit()

        payment_method = booking.payment_method
        if booking.tutor:
            tutor_name = booking.tutor.name

        tg_username = booking.student.telegram_username if booking.student else None
        if booking.student and booking.student.telegram_id:
            student_tg_id = booking.student.telegram_id
        appt_text = fmt_full(booking.appointment_time)
        service_name = booking.service_type

    kb_rows = []
    if tg_username:
        clean = tg_username.lstrip("@")
        kb_rows.append([InlineKeyboardButton(text="💬 Написать ученику", url=f"https://t.me/{clean}")])

    if payment_method == "cash":
        tutor_msg = (
            f"🔴 <b>Запись отклонена</b>\n\n"
            f"Вы можете связаться с учеником для уточнения деталей."
        )
        tutor_toast = "Запись отклонена 🔴"
        student_msg = (
            f"🔴 <b>Ваша запись отклонена</b>\n\n"
            f"Преподаватель: <b>{tutor_name}</b>\n"
            f"🕒 {appt_text}\n"
            f"📚 {service_name}\n\n"
            f"<i>Преподаватель отклонил вашу запись. "
            f"Свяжитесь с преподавателем для уточнения деталей.</i>"
        )
    else:
        tutor_msg = (
            f"🔴 <b>Оплата не подтверждена, запись отклонена</b>\n\n"
            f"Вы можете связаться с учеником для уточнения деталей."
        )
        tutor_toast = "Запись отклонена 🔴"
        student_msg = (
            f"🔴 <b>Ваша запись отклонена</b>\n\n"
            f"Преподаватель: <b>{tutor_name}</b>\n"
            f"🕒 {appt_text}\n"
            f"📚 {service_name}\n\n"
            f"<i>Оплата не была подтверждена преподавателем. "
            f"Свяжитесь с преподавателем для уточнения деталей.</i>"
        )

    await callback.message.edit_text(
        tutor_msg,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None,
    )
    await callback.answer(tutor_toast)
    logger.info("Booking #%d rejected by tutor tg_id=%d", booking_id, callback.from_user.id)

    # Notify student
    if student_tg_id:
        from app.core.bot import get_bot
        bot = get_bot()
        if bot:
            try:
                await bot.send_message(
                    chat_id=student_tg_id,
                    text=student_msg,
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.error("Failed to notify student tg_id=%d of rejection: %s", student_tg_id, exc)


# ═════════════════════════════════════════════════════════════════════
#  🔐 Admin: /extend_sub — Extend a tutor’s subscription
# ═════════════════════════════════════════════════════════════════════


@router.message(Command("extend_sub"))
async def cmd_extend_sub(message: Message) -> None:
    """Административная команда для продления подписки репетитора.

    Использование: /extend_sub <tutor_id> <days>
    Пример:  /extend_sub 1 30
    """
    from app.core.config import settings

    # Only platform admin can use this
    if settings.admin_tg_id is None or message.from_user.id != settings.admin_tg_id:
        await message.answer("⛔ Эта команда доступна только администратору платформы.")
        return

    parts = message.text.strip().split()
    if len(parts) != 3:
        await message.answer(
            "❌ <b>Использование:</b> <code>/extend_sub &lt;tutor_id&gt; &lt;days&gt;</code>\n"
            "Пример: <code>/extend_sub 1 30</code>",
            parse_mode="HTML",
        )
        return

    try:
        tutor_id = int(parts[1])
        days = int(parts[2])
    except ValueError:
        await message.answer("❌ tutor_id и days должны быть целыми числами.")
        return

    if days <= 0:
        await message.answer("❌ Количество дней должно быть положительным.")
        return

    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor is None:
            await message.answer(f"❌ Репетитор с ID {tutor_id} не найден.")
            return

        now = datetime.now(timezone.utc)
        # Extend from current expiry if still active, otherwise from today
        sub_expires = tutor.subscription_expires_at
        if sub_expires and sub_expires.tzinfo is None:
            sub_expires = sub_expires.replace(tzinfo=timezone.utc)
        if sub_expires and sub_expires > now:
            tutor.subscription_expires_at = sub_expires + timedelta(days=days)
        else:
            tutor.subscription_expires_at = now + timedelta(days=days)
        tutor.subscription_status = "active"
        await session.commit()

        expires_text = fmt_full(tutor.subscription_expires_at)
        tutor_name = tutor.name
        tutor_db_id = tutor.id

    await message.answer(
        f"✅ <b>Подписка продлена!</b>\n\n"
        f"👤 Репетитор: <b>{tutor_name}</b> (ID: {tutor_db_id})\n"
        f"📅 Дата окончания: <b>{expires_text}</b>\n"
        f"➕ Добавлено дней: {days}",
        parse_mode="HTML",
    )
    logger.info("Admin extended subscription for tutor_id=%d by %d days", tutor_db_id, days)


# ═════════════════════════════════════════════════════════════════════
#  💳 Tutor Subscription & Admin Panel
# ═════════════════════════════════════════════════════════════════════

class AdminSubManualStates(StatesGroup):
    waiting_tutor_id_days = State()
    waiting_tutor_id_revoke = State()


@router.callback_query(F.data == "tutor_sub_pay")
async def cb_tutor_sub_pay(callback: CallbackQuery, state: FSMContext) -> None:
    from app.core.config import settings
    admin_phone = settings.admin_sbp_phone or "+79001234567"
    admin_bank = settings.admin_sbp_bank or "Сбербанк"

    await state.set_state(TutorSubStates.waiting_payer_name)
    await callback.message.answer(
        "📱 <b>Оплата подписки AcademicLink (990 ₽/мес)</b>\n\n"
        f"Для продления переведите <b>990 ₽</b> по СБП на реквизиты администратора:\n"
        f"📞 Телефон: <code>{admin_phone}</code>\n"
        f"🏦 Банк: <b>{admin_bank}</b>\n\n"
        "После перевода, пожалуйста, <b>введите имя и отчество отправителя</b> (например, <i>Иван И.</i>), чтобы мы могли идентифицировать ваш перевод:\n\n"
        "<i>Для отмены пришлите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(TutorSubStates.waiting_payer_name)
async def process_tutor_sub_payer_name(message: Message, state: FSMContext) -> None:
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Оплата подписки отменена.")
        return

    payer_name = message.text.strip()
    if len(payer_name) < 2 or len(payer_name) > 100:
        await message.answer("❌ Пожалуйста, введите корректное имя отправителя (от 2 до 100 символов).")
        return

    await state.clear()
    
    # Get tutor details
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor is None:
            await message.answer("❌ Репетитор не найден.")
            return
        tutor_id = tutor.id
        tutor_name = tutor.name
        tutor_username = message.from_user.username or "нет"

    # Send notification to admin
    from app.core.config import settings
    from app.core.bot import get_bot
    bot = get_bot()
    
    if settings.admin_tg_id and bot:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🟢 Подтвердить", callback_data=f"admin_sub_app:{tutor_id}"),
                InlineKeyboardButton(text="🔴 Отклонить", callback_data=f"admin_sub_rej:{tutor_id}")
            ]
        ])
        
        try:
            await bot.send_message(
                chat_id=settings.admin_tg_id,
                text=f"💳 <b>Новая заявка на подписку!</b>\n\n"
                     f"👤 Репетитор: <b>{tutor_name}</b> (@{tutor_username})\n"
                     f"🆔 ID: {tutor_id}\n"
                     f"💰 Сумма: <b>990 ₽</b>\n"
                     f"👤 Отправитель перевода: <b>{payer_name}</b>\n\n"
                     f"Проверьте поступление средств на вашу карту СБП!",
                parse_mode="HTML",
                reply_markup=kb
            )
            await message.answer(
                "✅ <b>Заявка отправлена администратору!</b>\n\n"
                "Мы свяжемся с вами и продлим подписку, как только проверим перевод.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error("Failed to send subscription request to admin: %s", e)
            await message.answer("❌ Произошла ошибка при отправке заявки администратору. Попробуйте еще раз позже.")
    else:
        await message.answer("❌ Администратор платформы не настроен. Обратитесь в техподдержку.")


@router.callback_query(F.data.startswith("admin_sub_app:"))
async def cb_admin_sub_approve(callback: CallbackQuery) -> None:
    from app.core.config import settings
    if settings.admin_tg_id is None or callback.from_user.id != settings.admin_tg_id:
        await callback.answer("⛔ Вы не являетесь администратором.", show_alert=True)
        return

    tutor_id = int(callback.data.split(":")[1])
    
    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor is None:
            await callback.answer("❌ Репетитор не найден.", show_alert=True)
            return

        now = datetime.now(timezone.utc)
        sub_expires = tutor.subscription_expires_at
        if sub_expires and sub_expires.tzinfo is None:
            sub_expires = sub_expires.replace(tzinfo=timezone.utc)
            
        if sub_expires and sub_expires > now:
            tutor.subscription_expires_at = sub_expires + timedelta(days=30)
        else:
            tutor.subscription_expires_at = now + timedelta(days=30)
            
        tutor.subscription_status = "active"
        tutor.subscription_warned_at = None
        await session.commit()
        
        tutor_name = tutor.name
        tutor_tg_id = tutor.tg_id
        expires_text = fmt_full(tutor.subscription_expires_at)

    await callback.message.edit_text(
        f"✅ <b>Подписка репетитора {tutor_name} (ID: {tutor_id}) одобрена!</b>\n\n"
        f"📅 Новая дата окончания: <b>{expires_text}</b>",
        parse_mode="HTML"
    )
    await callback.answer("Подписка продлена на 30 дней.")
    
    # Notify tutor
    from app.core.bot import get_bot
    bot = get_bot()
    if bot and tutor_tg_id:
        try:
            await bot.send_message(
                chat_id=tutor_tg_id,
                text=f"✅ <b>Ваша подписка успешно продлена на 30 дней!</b>\n\n"
                     f"Доступ к расписанию разблокирован до <b>{expires_text}</b>. Спасибо!",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error("Failed to notify tutor of approved subscription: %s", e)


@router.callback_query(F.data.startswith("admin_sub_rej:"))
async def cb_admin_sub_reject(callback: CallbackQuery) -> None:
    from app.core.config import settings
    if settings.admin_tg_id is None or callback.from_user.id != settings.admin_tg_id:
        await callback.answer("⛔ Вы не являетесь администратором.", show_alert=True)
        return

    tutor_id = int(callback.data.split(":")[1])
    
    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor is None:
            await callback.answer("❌ Репетитор не найден.", show_alert=True)
            return
        tutor_name = tutor.name
        tutor_tg_id = tutor.tg_id

    await callback.message.edit_text(
        f"❌ <b>Заявка на подписку репетитора {tutor_name} (ID: {tutor_id}) отклонена.</b>",
        parse_mode="HTML"
    )
    await callback.answer("Заявка отклонена.")
    
    # Notify tutor
    from app.core.bot import get_bot
    bot = get_bot()
    if bot and tutor_tg_id:
        try:
            await bot.send_message(
                chat_id=tutor_tg_id,
                text="🔴 <b>Ваша заявка на продление подписки была отклонена.</b>\n\n"
                     "Если это ошибка, пожалуйста, проверьте реквизиты перевода СБП и повторите отправку.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error("Failed to notify tutor of rejected subscription: %s", e)


# 🔐 Admin: /admin command
@router.message(Command("admin"))
async def cmd_admin_panel(message: Message) -> None:
    from app.core.config import settings
    if settings.admin_tg_id is None or message.from_user.id != settings.admin_tg_id:
        await message.answer("⛔ Эта команда доступна только администратору платформы.")
        return

    async with async_session_factory() as session:
        # Calculate stats
        tutors_res = await session.execute(select(Tutor))
        tutors = tutors_res.scalars().all()
        
        now = datetime.now(timezone.utc)
        active_subs = 0
        expired_subs = 0
        trial_subs = 0
        tutors_list_lines = []
        
        for t in tutors:
            if t.subscription_expires_at is None:
                expired_subs += 1
                status_desc = "🔴 Без подписки"
            else:
                sub_expires = t.subscription_expires_at
                if sub_expires.tzinfo is None:
                    sub_expires = sub_expires.replace(tzinfo=timezone.utc)
                if sub_expires > now:
                    expires_str = sub_expires.astimezone(MSK).strftime("%d.%m.%Y")
                    if t.subscription_status == "trial":
                        trial_subs += 1
                        status_desc = f"🟡 Пробная (до {expires_str})"
                    else:
                        active_subs += 1
                        status_desc = f"🟢 Активна (до {expires_str})"
                else:
                    expired_subs += 1
                    status_desc = "🔴 Истекла"
            
            tutors_list_lines.append(f"• ID: <code>{t.id}</code> — <b>{t.name}</b> ({status_desc})")

        total_tutors = len(tutors)
        tutors_list_str = "\n".join(tutors_list_lines) if tutors_list_lines else "<i>Репетиторы еще не зарегистрированы.</i>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Выдать подписку", callback_data="admin_sub_give_manual"),
            InlineKeyboardButton(text="➖ Аннулировать подписку", callback_data="admin_sub_revoke_manual")
        ]
    ])

    await message.answer(
        f"👑 <b>Панель администратора AcademicLink</b>\n\n"
        f"📊 <b>Статистика репетиторов:</b>\n"
        f"👥 Всего репетиторов: <b>{total_tutors}</b>\n"
        f"🟢 Активных подписок: <b>{active_subs}</b>\n"
        f"🟡 Пробных (trial): <b>{trial_subs}</b>\n"
        f"🔴 Истекших/без подписки: <b>{expired_subs}</b>\n\n"
        f"📋 <b>Список репетиторов:</b>\n"
        f"{tutors_list_str}\n\n"
        f"📝 <b>Команды для ручного управления:</b>\n"
        f"• <code>/extend_sub &lt;tutor_id&gt; &lt;days&gt;</code> — продлить подписку репетитору.\n"
        f"• <code>/revoke_sub &lt;tutor_id&gt;</code> — аннулировать подписку репетитора.",
        parse_mode="HTML",
        reply_markup=kb
    )


@router.callback_query(F.data == "admin_sub_give_manual")
async def cb_admin_sub_give_manual(callback: CallbackQuery, state: FSMContext) -> None:
    from app.core.config import settings
    if settings.admin_tg_id is None or callback.from_user.id != settings.admin_tg_id:
        await callback.answer("⛔ Вы не являетесь администратором.", show_alert=True)
        return

    await state.set_state(AdminSubManualStates.waiting_tutor_id_days)
    await callback.message.answer(
        "📝 <b>Ручная выдача подписки</b>\n\n"
        "Отправьте ID репетитора и количество дней через пробел.\n"
        "Например: <code>12 30</code> (продлить репетитору с ID 12 на 30 дней).\n\n"
        "<i>Для отмены пришлите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminSubManualStates.waiting_tutor_id_days)
async def process_admin_sub_give_manual(message: Message, state: FSMContext) -> None:
    from app.core.config import settings
    if settings.admin_tg_id is None or message.from_user.id != settings.admin_tg_id:
        await state.clear()
        return

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Ручная выдача подписки отменена.")
        return

    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer("❌ Неверный формат. Введите ID репетитора и количество дней через пробел (например: <code>12 30</code>):", parse_mode="HTML")
        return

    try:
        tutor_id = int(parts[0])
        days = int(parts[1])
    except ValueError:
        await message.answer("❌ ID репетитора и количество дней должны быть целыми числами. Попробуйте еще раз:")
        return

    if days <= 0:
        await message.answer("❌ Количество дней должно быть больше 0. Попробуйте еще раз:")
        return

    await state.clear()

    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor is None:
            await message.answer(f"❌ Репетитор с ID {tutor_id} не найден.")
            return

        now = datetime.now(timezone.utc)
        sub_expires = tutor.subscription_expires_at
        if sub_expires and sub_expires.tzinfo is None:
            sub_expires = sub_expires.replace(tzinfo=timezone.utc)
            
        if sub_expires and sub_expires > now:
            tutor.subscription_expires_at = sub_expires + timedelta(days=days)
        else:
            tutor.subscription_expires_at = now + timedelta(days=days)
            
        tutor.subscription_status = "active"
        await session.commit()
        
        tutor_name = tutor.name
        tutor_tg_id = tutor.tg_id
        expires_text = fmt_full(tutor.subscription_expires_at)

    await message.answer(
        f"✅ <b>Подписка успешно выдана!</b>\n\n"
        f"👤 Репетитор: <b>{tutor_name}</b> (ID: {tutor_id})\n"
        f"📅 Новая дата окончания: <b>{expires_text}</b>\n"
        f"➕ Добавлено дней: {days}",
        parse_mode="HTML"
    )
    
    # Notify tutor
    from app.core.bot import get_bot
    bot = get_bot()
    if bot and tutor_tg_id:
        try:
            await bot.send_message(
                chat_id=tutor_tg_id,
                text=f"✅ <b>Вам выдана подписка администратором!</b>\n\n"
                     f"Доступ к расписанию продлен на <b>{days} дн.</b> (до <b>{expires_text}</b>).",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error("Failed to notify tutor of manually granted subscription: %s", e)


@router.message(Command("revoke_sub"))
async def cmd_revoke_sub(message: Message) -> None:
    """Административная команда для аннулирования подписки репетитора.

    Использование: /revoke_sub <tutor_id>
    Пример:  /revoke_sub 1
    """
    from app.core.config import settings

    if settings.admin_tg_id is None or message.from_user.id != settings.admin_tg_id:
        await message.answer("⛔ Эта команда доступна только администратору платформы.")
        return

    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer(
            "❌ <b>Использование:</b> <code>/revoke_sub &lt;tutor_id&gt;</code>\n"
            "Пример: <code>/revoke_sub 1</code>",
            parse_mode="HTML",
        )
        return

    try:
        tutor_id = int(parts[1])
    except ValueError:
        await message.answer("❌ tutor_id должен быть целым числом.")
        return

    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor is None:
            await message.answer(f"❌ Репетитор с ID {tutor_id} не найден.")
            return

        tutor.subscription_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        tutor.subscription_status = "expired"
        await session.commit()
        
        tutor_name = tutor.name
        tutor_tg_id = tutor.tg_id

    await message.answer(
        f"✅ <b>Подписка успешно аннулирована!</b>\n\n"
        f"👤 Репетитор: <b>{tutor_name}</b> (ID: {tutor_id})\n"
        f"🔒 Доступ к расписанию заблокирован.",
        parse_mode="HTML"
    )
    logger.info("Admin revoked subscription for tutor_id=%d", tutor_id)

    # Notify tutor
    from app.core.bot import get_bot
    bot = get_bot()
    if bot and tutor_tg_id:
        try:
            await bot.send_message(
                chat_id=tutor_tg_id,
                text="🔴 <b>Ваша подписка на AcademicLink была аннулирована администратором.</b>\n\n"
                     "Доступ к расписанию и функциям заблокирован.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e:
            logger.error("Failed to notify tutor of revoked subscription: %s", e)


@router.callback_query(F.data == "admin_sub_revoke_manual")
async def cb_admin_sub_revoke_manual(callback: CallbackQuery, state: FSMContext) -> None:
    from app.core.config import settings
    if settings.admin_tg_id is None or callback.from_user.id != settings.admin_tg_id:
        await callback.answer("⛔ Вы не являетесь администратором.", show_alert=True)
        return

    await state.set_state(AdminSubManualStates.waiting_tutor_id_revoke)
    await callback.message.answer(
        "📝 <b>Аннулирование подписки</b>\n\n"
        "Отправьте ID репетитора, подписку которого хотите аннулировать.\n"
        "Например: <code>12</code> (аннулировать подписку репетитору с ID 12).\n\n"
        "<i>Для отмены пришлите /cancel</i>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(AdminSubManualStates.waiting_tutor_id_revoke)
async def process_admin_sub_revoke_manual(message: Message, state: FSMContext) -> None:
    from app.core.config import settings
    if settings.admin_tg_id is None or message.from_user.id != settings.admin_tg_id:
        await state.clear()
        return

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Аннулирование подписки отменено.")
        return

    try:
        tutor_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ ID репетитора должен быть целым числом. Попробуйте еще раз:")
        return

    await state.clear()

    async with async_session_factory() as session:
        tutor = await session.get(Tutor, tutor_id)
        if tutor is None:
            await message.answer(f"❌ Репетитор с ID {tutor_id} не найден.")
            return

        tutor.subscription_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        tutor.subscription_status = "expired"
        await session.commit()
        
        tutor_name = tutor.name
        tutor_tg_id = tutor.tg_id

    await message.answer(
        f"✅ <b>Подписка успешно аннулирована!</b>\n\n"
        f"👤 Репетитор: <b>{tutor_name}</b> (ID: {tutor_id})\n"
        f"🔒 Доступ к расписанию заблокирован.",
        parse_mode="HTML"
    )
    
    # Notify tutor
    from app.core.bot import get_bot
    bot = get_bot()
    if bot and tutor_tg_id:
        try:
            await bot.send_message(
                chat_id=tutor_tg_id,
                text="🔴 <b>Ваша подписка на AcademicLink была аннулирована администратором.</b>\n\n"
                     "Доступ к расписанию и функциям заблокирован.",
                parse_mode="HTML",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e:
            logger.error("Failed to notify tutor of revoked subscription: %s", e)
