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
from app.db.models import AvailabilitySlot, Booking, BookingStatus, Service, Student, Tutor, TutorAbsence

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
    waiting_reason = State()


class SlotManagement(StatesGroup):
    entering_times = State()


class BroadcastStates(StatesGroup):
    composing = State()


class RescheduleStates(StatesGroup):
    waiting_datetime = State()


class ManualBookingStates(StatesGroup):
    waiting_datetime = State()
    waiting_service_type = State()


class TutorSettingsStates(StatesGroup):
    waiting_meeting_link = State()


class ServiceManagement(StatesGroup):
    waiting_name = State()
    waiting_duration = State()
    waiting_buffer = State()
    waiting_price = State()


# ── Helpers ──────────────────────────────────────────────────────────


async def _get_tutor(tg_id: int, session) -> Tutor | None:
    result = await session.execute(select(Tutor).where(Tutor.tg_id == tg_id))
    return result.scalar_one_or_none()


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
    await _send_dashboard(message)


# ═════════════════════════════════════════════════════════════════════
#  🏠 Главная / /start — dynamic dashboard
# ═════════════════════════════════════════════════════════════════════


async def _send_dashboard(message: Message) -> None:
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
                select(func.count(func.distinct(Booking.student_id))).where(
                    Booking.tutor_id == tutor.id,
                    Booking.student_id.in_(
                        select(Student.id).where(Student.is_active == True)
                    )
                )
            )
            total_students = students_res.scalar_one()

            status_icon = "🟢" if tutor.is_active else "🔴"
            status_text = "Активен" if tutor.is_active else "Пауза"

            text = (
                f"{_greeting()}, <b>{tutor.name}</b>!\n\n"
                f"👤 {tutor.name}  ·  {status_icon} {status_text}\n\n"
                f"📅 Подтверждено на сегодня: <b>{today_confirmed}</b>\n"
                f"🟡 Новые заявки: <b>{pending_count}</b>\n"
                f"👥 Всего учеников: <b>{total_students}</b>\n\n"
                                "<i>Выберите действие из меню ниже:</i>"
            )
            
            await message.answer(text, parse_mode="HTML", reply_markup=MAIN_MENU)
            return

        # ── 2. Handle Student Automatic Linking ──────────────────────
        if username:
            stmt = select(Student).where(
                Student.telegram_username == username,
                Student.telegram_id.is_(None)
            )
            result = await session.execute(stmt)
            student = result.scalar_one_or_none()

            if student:
                student.telegram_id = tg_id
                await session.commit()
                await message.answer(
                    f"👋 Привет, <b>{student.full_name}</b>!\n\n"
                    f"Я — бот системы <b>AcademicLink</b>. Теперь вы будете получать "
                    f"уведомления и напоминания о ваших занятиях прямо здесь.\n\n"
                    f"✅ Ваш профиль успешно привязан.",
                    parse_mode="HTML"
                )
                return

        # ── 3. Onboarding: Automatically register as Tutor ───────────
        from datetime import time
        from app.core.config import settings

        # Create Tutor
        tutor = Tutor(
            tg_id=tg_id,
            name=name,
            is_active=True,
            lesson_duration=60,
            buffer_time=15,
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
        )
        session.add(default_service)

        # Create default availability slots (Mon-Fri 09:00 - 18:00)
        for day in range(5):  # 0 = Monday, 4 = Friday
            slot = AvailabilitySlot(
                tutor_id=tutor.id,
                day_of_week=day,
                start_time=time(9, 0),
                end_time=time(18, 0),
            )
            session.add(slot)

        await session.commit()

        # Beautiful greeting message with personal link and button
        text = (
            f"🎉 <b>Добро пожаловать в AcademicLink, {name}!</b>\n\n"
            f"Я автоматически зарегистрировал вас как репетитора и создал стартовую услугу «Консультация».\n\n"
            f"🔗 <b>Ваша персональная ссылка для записи учеников:</b>\n"
            f"{settings.web_url}/book/{tutor.id}\n\n"
            f"⚙️ <b>Настройки:</b>\n"
            f"Вы можете настроить свои услуги, время работы и календарь с помощью меню.\n"
            f"Давайте подключим Google Календарь для синхронизации!"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Подключить Google Календарь", url=f"{settings.web_url}/auth/google/login/{tutor.id}")]
        ])
        
        await message.answer(text, parse_mode="HTML", reply_markup=MAIN_MENU)
        await message.answer("Рекомендуем сразу настроить интеграцию:", reply_markup=kb)
        return


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.text and "gcal_success" in message.text:
        await message.answer("🎉 <b>Google Календарь успешно подключен!</b>\n\nТеперь все новые записи будут автоматически появляться в вашем календаре, а занятые слоты будут блокироваться на лендинге.", parse_mode="HTML")
    await _send_dashboard(message)


@router.message(F.text == "🏠 Главная")
async def cmd_home(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _send_dashboard(message)


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

        result = await session.execute(
            select(Booking)
            .where(
                Booking.tutor_id == tutor.id,
                Booking.status.in_(statuses),
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
    text, kb = await _build_bookings_page(
        message.from_user.id, 0, [BookingStatus.CONFIRMED], "📅 <b>Расписание</b>", "page_sch"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb or MAIN_MENU)


@router.message(F.text == "🟡 Новые заявки")
async def cmd_new_requests(message: Message, state: FSMContext) -> None:
    await state.clear()
    text, kb = await _build_bookings_page(
        message.from_user.id, 0, [BookingStatus.PENDING], "🟡 <b>Новые заявки</b>", "page_new"
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
            await message.answer(_NOT_REGISTERED, parse_mode="HTML")
            return

        # Distinct students via a grouped subquery on student_id
        result = await session.execute(
            select(Student)
            .where(
                Student.is_active == True,
                Student.id.in_(
                    select(Booking.student_id)
                    .where(Booking.tutor_id == tutor.id)
                    .distinct()
                )
            )
            .order_by(Student.full_name)
        )
        students = result.scalars().all()

    if not students:
        await message.answer(
            "👥 <b>Ученики</b>\n\n"
            "У вас пока нет учеников.\n"
            "Они появятся здесь после первой записи.",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    lines = [f"👥 <b>Ученики</b>  ({len(students)})\n"]
    for s in students:
        lines.append(f"👤 <b>{s.full_name}</b>")
        lines.append(f"     📞 {s.phone}\n")

    # Build inline buttons: View History + Contact per student
    kb_rows = []
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


@router.callback_query(F.data.startswith("student_history:"))
async def cb_student_history(callback: CallbackQuery) -> None:
    """Show booking history for a specific student."""
    student_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        result = await session.execute(
            select(Student)
            .where(Student.id == student_id)
            .options(selectinload(Student.bookings))
        )
        student = result.scalar_one_or_none()

    if student is None:
        await callback.answer("Ученик не найден.", show_alert=True)
        return

    bookings = sorted(student.bookings, key=lambda b: b.appointment_time, reverse=True)

    lines = [
        f"👤 <b>{student.full_name}</b>",
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

    # Build inline contact button + book lesson + delete button
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

    await callback.message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()


@router.message(StudentSearch.waiting_phone)
async def process_student_phone(message: Message, state: FSMContext) -> None:
    phone = message.text.strip()

    if len(phone) < 7 or not any(c.isdigit() for c in phone):
        await message.answer(
            "Введите корректный номер телефона.\n"
            "<i>Например: +998901234567</i>",
            parse_mode="HTML",
            reply_markup=BACK_KB,
        )
        return

    await state.clear()
    await _show_student_card(message, phone)


@router.message(Command("student"))
async def cmd_student_direct(message: Message, state: FSMContext) -> None:
    """Direct /student +998... command (bypasses FSM)."""
    await state.clear()
    parts = message.text.strip().split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "<b>Использование:</b> <code>/student +998901234567</code>",
            parse_mode="HTML",
        )
        return
    await _show_student_card(message, parts[1].strip())


async def _show_student_card(message: Message, phone: str) -> None:
    async with async_session_factory() as session:
        result = await session.execute(
            select(Student)
            .where(
                Student.phone == phone,
                Student.is_active == True,
            )
            .options(selectinload(Student.bookings))
        )
        student = result.scalar_one_or_none()

    if student is None:
        await message.answer(
            f"Ученик с номером <code>{phone}</code> не найден.\n\n"
            "<i>Проверьте номер и попробуйте ещё раз.</i>",
            parse_mode="HTML",
            reply_markup=MAIN_MENU,
        )
        return

    bookings = sorted(student.bookings, key=lambda b: b.appointment_time, reverse=True)

    lines = [
        f"👤 <b>{student.full_name}</b>",
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

    # Build inline contact button + book lesson + delete button
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

    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )


# ═════════════════════════════════════════════════════════════════════
# ⚙️ Настройки — profile + toggle
# ═════════════════════════════════════════════════════════════════════


def _settings_text(tutor: Tutor, slots: list[AvailabilitySlot]) -> str:
    icon = "🟢" if tutor.is_active else "🔴"
    status = "Активен" if tutor.is_active else "Неактивен"

    days_map = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"}
    by_day: dict[int, list[AvailabilitySlot]] = {}
    for s in slots:
        by_day.setdefault(s.weekday, []).append(s)

    schedule_lines = []
    for day in sorted(by_day):
        windows = ", ".join(f"{s.start_time:%H:%M}–{s.end_time:%H:%M}" for s in by_day[day])
        schedule_lines.append(f"  {days_map[day]}: {windows}")
    
    schedule_text = "\n".join(schedule_lines) if schedule_lines else "<i>Слоты не настроены</i>"

    link_text = f"🔗 <b>Zoom/Meet:</b> {tutor.meeting_link or '<i>не установлена</i>'}"
    remind_icon = "🔔" if tutor.wants_reminders else "🔕"
    gcal_status = "🟢 Подключен" if tutor.google_token_json else "🔴 Не подключен"

    return (
        f"⚙️ <b>Настройки</b>\n\n"
        f"👤 <b>{tutor.name}</b>  ·  {icon} {status}\n"
        f"{link_text}\n"
        f"📅 <b>Google Календарь:</b> {gcal_status}\n\n"
        f"⏰ <b>Рабочие часы:</b>\n"
        f"{schedule_text}\n\n"
        f"{remind_icon} <b>Напоминания:</b> {'Вкл' if tutor.wants_reminders else 'Откл'}\n"
    )


def _settings_kb(tutor: Tutor) -> InlineKeyboardMarkup:
    toggle_text = "🔴 Пауза" if tutor.is_active else "🟢 Старт"
    remind_text = "🔕 Увед." if tutor.wants_reminders else "🔔 Увед."
    
    from app.core.config import settings
    
    gcal_btn = (
        InlineKeyboardButton(text="📅 Отключить Google", callback_data="gcal_disconnect")
        if tutor.google_token_json
        else InlineKeyboardButton(text="📅 Подключить Google", url=f"{settings.web_url}/auth/google/login/{tutor.id}")
    )
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=toggle_text, callback_data=f"toggle:{tutor.id}"),
            InlineKeyboardButton(text=remind_text, callback_data=f"toggle_remind:{tutor.id}"),
        ],
        [
            InlineKeyboardButton(text="💎 Мои услуги", callback_data="manage_services"),
            InlineKeyboardButton(text="🔗 Ссылка Zoom", callback_data="set_meeting_link"),
        ],
        [
            InlineKeyboardButton(text="⏰ Мои слоты", callback_data="manage_slots"),
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



# ═════════════════════════════════════════════════════════════════════
#  📅 Отсутствие — sick leave / vacation management
# ═════════════════════════════════════════════════════════════════════


@router.message(F.text == "📅 Отсутствие")
async def cmd_absence(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor is None:
            await message.answer(_NOT_REGISTERED, parse_mode="HTML")
            return

        # Show upcoming absences
        now = datetime.now(timezone.utc)
        result = await session.execute(
            select(TutorAbsence)
            .where(
                TutorAbsence.tutor_id == tutor.id,
                TutorAbsence.end_time >= now
            )
            .order_by(TutorAbsence.start_time)
        )
        absences = result.scalars().all()

    lines = ["📅 <b>Моё отсутствие</b>\n"]
    if not absences:
        lines.append("У вас нет запланированных периодов отсутствия.")
    else:
        for a in absences:
            reason = f" ({a.reason})" if a.reason else ""
            lines.append(
                f"• {fmt_full(a.start_time)} — {fmt_full(a.end_time)}{reason}\n"
                f"  /del_absence_{a.id}"
            )

    lines.append("\n<i>Добавьте период (болезнь, отпуск), чтобы временно закрыть запись.</i>")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить период", callback_data="add_absence_init")],
        [InlineKeyboardButton(text="⚡️ Занять время (сегодня)", callback_data="quick_block_today")],
    ])

    await message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)


# ── Absence Management Callbacks ─────────────────────────────────────


@router.callback_query(F.data == "quick_block_today")
async def cb_quick_block_today(callback: CallbackQuery) -> None:
    """Quickly block the rest of the current day."""
    now_local = datetime.now(MSK)
    today_weekday = now_local.weekday()

    async with async_session_factory() as session:
        tutor = await _get_tutor(callback.from_user.id, session)

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
            await callback.answer("Сегодня нет рабочих слотов.", show_alert=True)
            return

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
        for b in overlapping:
            b.status = BookingStatus.CANCELLED
            if b.student and b.student.telegram_id and bot:
                try:
                    await bot.send_message(
                        chat_id=b.student.telegram_id,
                        text=f"🔴 <b>Занятие отменено</b>\n\nРепетитор занят сегодня до конца дня.\nВаша запись на {fmt_full(b.appointment_time)} отменена.",
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

    await state.update_data(end_time=dt_utc.isoformat())
    await state.set_state(TutorAbsenceStates.waiting_reason)
    await message.answer(
        f"✅ Окончание: <b>{fmt_full(dt_utc)}</b>\n\n"
        "📝 <b>Введите причину</b> (необязательно)\n"
        "<i>Например: Болезнь или Отпуск</i>",
        parse_mode="HTML",
        reply_markup=BACK_KB
    )


@router.message(TutorAbsenceStates.waiting_reason)
async def process_absence_reason(message: Message, state: FSMContext) -> None:
    reason = message.text.strip()
    if reason == "◀️ Назад": # Shouldn't happen if handled globally but just in case
        return

    data = await state.get_data()
    start_time = datetime.fromisoformat(data["start_time"])
    end_time = datetime.fromisoformat(data["end_time"])

    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        
        absence = TutorAbsence(
            tutor_id=tutor.id,
            start_time=start_time,
            end_time=end_time,
            reason=reason if reason.lower() != "пропустить" else None
        )
        session.add(absence)
        
        # ── Handle Overlapping Bookings ──────────────────────────────
        stmt = select(Booking).where(
            Booking.tutor_id == tutor.id,
            Booking.status.in_([BookingStatus.PENDING, BookingStatus.CONFIRMED]),
            Booking.appointment_time >= start_time,
            Booking.appointment_time < end_time
        ).options(selectinload(Booking.student))
        
        result = await session.execute(stmt)
        overlapping = result.scalars().all()
        
        cancelled_count = len(overlapping)
        from app.core.bot import get_bot
        bot = get_bot()

        for b in overlapping:
            b.status = BookingStatus.CANCELLED
            
            # Notify student
            if b.student and b.student.telegram_id and bot:
                student_text = (
                    f"🔴 <b>Занятие отменено</b>\n\n"
                    f"К сожалению, репетитор будет отсутствовать с {fmt_full(start_time)} по {fmt_full(end_time)}.\n"
                    f"Причина: {reason}\n\n"
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
        reply_markup=MAIN_MENU
    )


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
        else:
            await message.answer("❌ Запись не найдена.")


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
        if booking.status != BookingStatus.PENDING:
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        booking.status = BookingStatus.CONFIRMED
        await session.commit()

        # Sync to Google Calendar
        from app.services.google_calendar_service import sync_booking_to_calendar
        try:
            await sync_booking_to_calendar(session, booking)
        except Exception as exc:
            logger.error("Failed to sync confirmed booking #%d to Google Calendar: %s", booking.id, exc)

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
    hours_until = (booking.appointment_time - now_utc).total_seconds() / 3600

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

        booking.status = BookingStatus.CANCELLED
        await session.commit()

        # Delete Google Calendar event
        from app.services.google_calendar_service import delete_calendar_event
        try:
            await delete_calendar_event(session, booking)
        except Exception as exc:
            logger.error("Failed to delete Google event for cancelled booking #%d: %s", booking.id, exc)

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
                f"🕒 {appt_text}\n"
                f"📚 {service}\n\n"
                f"<i>Свяжитесь с репетитором для уточнения деталей.</i>"
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
        student = await session.get(Student, student_id)
        if student is None:
            await callback.answer("Ученик не найден.", show_alert=True)
            return

        # Soft delete
        student.is_active = False

        # Cleanup: Cancel PENDING bookings
        result = await session.execute(
            select(Booking).where(
                Booking.student_id == student_id,
                Booking.status == BookingStatus.PENDING,
            )
        )
        pending_bookings = result.scalars().all()
        for b in pending_bookings:
            b.status = BookingStatus.CANCELLED
            # Here we could also log the reason if we had a reason field in Booking model
            # But the prompt says "Student removed from CRM." as the reason.
            # Assuming we might want to notify or just leave it.

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


@router.callback_query(F.data == "set_meeting_link")
async def cb_set_meeting_link(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TutorSettingsStates.waiting_meeting_link)
    await callback.message.answer(
        "🔗 <b>Ссылка на Zoom/Google Meet</b>\n\n"
        "Отправьте постоянную ссылку на вашу конференцию.\n"
        "Она будет автоматически отправляться ученикам в напоминаниях.",
        parse_mode="HTML",
        reply_markup=BACK_KB,
    )
    await callback.answer()


@router.message(TutorSettingsStates.waiting_meeting_link)
async def process_meeting_link(message: Message, state: FSMContext) -> None:
    link = message.text.strip()
    if not link.startswith(("http://", "https://")):
        await message.answer("❌ Ссылка должна начинаться с http:// или https://")
        return

    async with async_session_factory() as session:
        tutor = await _get_tutor(message.from_user.id, session)
        if tutor:
            tutor.meeting_link = link
            await session.commit()
            await message.answer(f"✅ Ссылка сохранена: <code>{link}</code>", parse_mode="HTML", reply_markup=MAIN_MENU)
        else:
            await message.answer("❌ Ошибка: Репетитор не найден.")

    await state.clear()


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
    if not services:
        lines.append("<i>Список услуг пуст. Добавьте первую услугу, чтобы открыть запись на сайте.</i>")
    else:
        for s in services:
            price_text = f" — {s.price} руб." if s.price else ""
            lines.append(f"• <b>{s.name}</b> ({s.duration} мин){price_text}\n  /del_service_{s.id}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить услугу", callback_data="add_service_init")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_settings")],
    ])

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
            if service.tutor_id == tutor.id:
                service.is_active = False # Soft delete
                await session.commit()
                await message.answer(f"🗑 Услуга «{service.name}» удалена.")
                await _show_service_manager(message, tutor.id)


# ═════════════════════════════════════════════════════════════════════
#  🗓 Перенос занятия (Reschedule)
# ═════════════════════════════════════════════════════════════════════


@router.callback_query(F.data.startswith("reschedule_init:"))
async def cb_reschedule_init(callback: CallbackQuery, state: FSMContext) -> None:
    """Start the reschedule flow — ask for a new date/time."""
    booking_id = int(callback.data.split(":")[1])

    async with async_session_factory() as session:
        booking = await session.get(Booking, booking_id)
        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
            await callback.answer("Эту запись нельзя перенести.", show_alert=True)
            return

    await state.set_state(RescheduleStates.waiting_datetime)
    await state.update_data(reschedule_booking_id=booking_id)

    await callback.message.answer(
        "🗓 <b>Перенос занятия</b>\n\n"
        "Введите новую дату и время (<b>ДД.ММ.ГГГГ ЧЧ:ММ</b>)\n"
        "<i>Например: 25.05.2026 14:00</i>",
        parse_mode="HTML",
        reply_markup=BACK_KB,
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
    await callback.message.edit_text(
        f"✅ <b>Занятие создано!</b>\n\n"
        f"👤 {student_name}\n"
        f"🕒 {fmt_full(appointment_time)}\n"
        f"📚 {booking.service_type}\n"
        f"🟢 Подтверждено",
        parse_mode="HTML"
    )
    await callback.message.answer("Главное меню", reply_markup=MAIN_MENU)
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
            select(func.count(func.distinct(Student.id))).where(
                Student.is_active == True,
                Student.id.in_(
                    select(Booking.student_id)
                    .where(Booking.tutor_id == tutor.id)
                    .distinct()
                ),
            )
        )
        total_students = result.scalar_one()

    await state.set_state(BroadcastStates.composing)
    await state.update_data(tutor_id=tutor.id)

    await message.answer(
        f"📢 <b>Рассылка</b>\n\n"
        f"У вас <b>{total_students}</b> учеников.\n"
        f"<i>(Внимание: Бот сможет отправить сообщение автоматически только тем ученикам, которые хотя бы раз запускали этого бота. В остальных случаях бот выдаст ошибку доставки, и им нужно будет написать вручную).</i>\n\n"
        f"Введите текст сообщения для рассылки.\n"
        f"<i>Поддерживается HTML-разметка: &lt;b&gt;жирный&lt;/b&gt;, &lt;i&gt;курсив&lt;/i&gt;</i>",
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

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data="broadcast_confirm"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="broadcast_cancel"),
        ]
    ])

    await message.answer(
        f"📢 <b>Предпросмотр рассылки:</b>\n\n"
        f"{'─' * 20}\n"
        f"{text}\n"
        f"{'─' * 20}\n\n"
        f"Отправить это сообщение всем вашим ученикам?",
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

    # Fetch ALL active students
    async with async_session_factory() as session:
        result = await session.execute(
            select(Student).where(
                Student.is_active == True,
                Student.id.in_(
                    select(Booking.student_id)
                    .where(Booking.tutor_id == tutor_id)
                    .distinct()
                ),
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
    
    for i, student in enumerate(students):
        if not student.telegram_id:
            no_tg_id += 1
            continue
            
        try:
            await bot.send_message(
                chat_id=student.telegram_id,
                text=broadcast_text,
                parse_mode="HTML",
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

    await callback.message.edit_text(
        f"📢 <b>Рассылка завершена!</b>\n\n"
        f"✅ Успешно доставлено: <b>{success}</b>\n"
        f"❌ Ошибка доставки (возможно заблокировали): <b>{failed}</b>\n"
        f"⚠️ Нет связи с ботом: <b>{no_tg_id}</b>\n\n"
        f"<i>Всего учеников: {len(students)}</i>",
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
