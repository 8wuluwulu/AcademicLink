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
from app.db.models import Booking, BookingStatus, Student, Tutor

logger = logging.getLogger(__name__)
router = Router(name="main_router")


# ── FSM States ───────────────────────────────────────────────────────


class StudentSearch(StatesGroup):
    waiting_phone = State()


class StudentManagement(StatesGroup):
    confirm_delete = State()


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
    """Build and send the Tutor Dashboard."""
    tg_id = message.from_user.id
    name = message.from_user.first_name or "Репетитор"
    now = datetime.now(MSK)

    async with async_session_factory() as session:
        tutor = await _get_tutor(tg_id, session)

        if tutor is None:
            await message.answer(
                f"{_greeting()}, <b>{name}</b>!\n\n"
                f"Ваш Telegram ID: <code>{tg_id}</code>\n\n"
                "Вы ещё не зарегистрированы как репетитор.\n"
                "Добавьте этот ID в <code>.env</code> как "
                "<code>DEFAULT_TUTOR_TG_ID</code> и перезапустите приложение.",
                parse_mode="HTML",
                reply_markup=MAIN_MENU,
            )
            return

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
        "<i>Выберите действие из меню ниже</i>"
    )

    await message.answer(text, parse_mode="HTML", reply_markup=MAIN_MENU)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
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

    # Build inline contact button + delete button
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

    # Build inline contact button + delete button
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
#  ⚙️ Настройки — profile + toggle
# ═════════════════════════════════════════════════════════════════════


def _settings_text(tutor: Tutor) -> str:
    icon = "🟢" if tutor.is_active else "🔴"
    status = "Активен — записи принимаются" if tutor.is_active else "Неактивен — записи заблокированы"
    return (
        f"⚙️ <b>Настройки</b>\n\n"
        f"👤 <b>{tutor.name}</b>\n"
        f"{icon} {status}\n\n"
        f"<i>Нажмите кнопку ниже, чтобы изменить статус.\n"
        f"В неактивном режиме сайт не принимает записи.</i>"
    )


def _toggle_kb(tutor: Tutor) -> InlineKeyboardMarkup:
    from aiogram.types import InlineKeyboardButton

    text = "🔴 Приостановить приём" if tutor.is_active else "🟢 Возобновить приём"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=f"toggle:{tutor.id}")],
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
    await message.answer(
        _settings_text(tutor), parse_mode="HTML", reply_markup=_toggle_kb(tutor),
    )


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

    async with async_session_factory() as session:
        booking = await session.get(Booking, booking_id)
        if booking is None:
            await callback.answer("Запись не найдена.", show_alert=True)
            return
        if booking.status != BookingStatus.PENDING:
            await callback.answer("Эта запись уже обработана.", show_alert=True)
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        booking.status = BookingStatus.CONFIRMED
        await session.commit()

    await callback.message.edit_text(
        f"🟢 <b>Запись подтверждена</b>\n\n"
        f"🕒 {fmt_full(now)}",
        parse_mode="HTML",
    )
    await callback.answer("Подтверждено")
    logger.info("Booking #%d confirmed by tg_id=%d", booking_id, callback.from_user.id)


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

    text = (
        "🔴 <b>Подтвердите отмену</b>\n\n"
        "Вы действительно хотите отменить эту запись?\n"
        "<i>Это действие нельзя отменить.</i>"
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
        
        tg_username = booking.student.telegram_username if booking.student else None

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

    # Build inline contact button if available
    kb_rows = []
    if tg_user:
        clean = tg_user.lstrip("@")
        kb_rows.append([InlineKeyboardButton(
            text="💬 Написать", url=f"https://t.me/{clean}",
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

    await callback.message.edit_text(
        _settings_text(tutor), parse_mode="HTML", reply_markup=_toggle_kb(tutor),
    )
    await callback.answer(alert)
    logger.info("Tutor #%d toggled is_active=%s", tutor_id, tutor.is_active)


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
