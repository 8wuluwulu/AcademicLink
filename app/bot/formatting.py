"""
AcademicLink — Bot Formatting & Keyboards

Centralized utilities for consistent, professional Telegram messages.
All user-facing strings are in Russian (business-style).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

if TYPE_CHECKING:
    from app.db.models import Booking

# ── Timezone ─────────────────────────────────────────────────────────
MSK = timezone(timedelta(hours=3))

# ── Visual Constants ─────────────────────────────────────────────────
PAGE_SIZE = 5

STATUS_EMOJI = {"PENDING": "🟡", "CONFIRMED": "🟢", "CANCELLED": "🔴"}
STATUS_LABEL = {
    "PENDING": "Ожидает",
    "CONFIRMED": "Подтверждена",
    "CANCELLED": "Отменена",
}

# ── Keyboards ────────────────────────────────────────────────────────

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🏠 Главная"), KeyboardButton(text="📅 Расписание")],
        [KeyboardButton(text="👥 Ученики"), KeyboardButton(text="⚙️ Настройки")],
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие…",
)

BACK_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="◀️ Назад")]],
    resize_keyboard=True,
    input_field_placeholder="Введите данные или нажмите Назад…",
)

# ── Russian locale data ─────────────────────────────────────────────

_WEEKDAYS = [
    "Понедельник", "Вторник", "Среда", "Четверг",
    "Пятница", "Суббота", "Воскресенье",
]
_WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MONTHS_GEN = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
_MONTHS_NOM = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


# ── Date / Time ──────────────────────────────────────────────────────


def fmt_date(dt: datetime) -> str:
    """'15 мая 2026, Четверг'"""
    lc = dt.astimezone(MSK)
    return f"{lc.day} {_MONTHS_GEN[lc.month]} {lc.year}, {_WEEKDAYS[lc.weekday()]}"


def fmt_date_short(dt: datetime) -> str:
    """'15 мая, Чт'"""
    lc = dt.astimezone(MSK)
    return f"{lc.day} {_MONTHS_GEN[lc.month]}, {_WEEKDAYS_SHORT[lc.weekday()]}"


def fmt_date_dot(dt: datetime) -> str:
    """'15.05.2026'"""
    return dt.astimezone(MSK).strftime("%d.%m.%Y")


def fmt_time(dt: datetime) -> str:
    """'14:00'"""
    return dt.astimezone(MSK).strftime("%H:%M")


def fmt_full(dt: datetime) -> str:
    """'15.05.2026 14:00'"""
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def fmt_month_year(dt: datetime) -> str:
    """'Май 2026'"""
    lc = dt.astimezone(MSK)
    return f"{_MONTHS_NOM[lc.month]} {lc.year}"


# ── Contact links ────────────────────────────────────────────────────


def fmt_phone_links(phone: str) -> str:
    """Build a clickable tel: link for a phone number."""
    return f'📞 <a href="tel:{phone}">{phone}</a>'


def fmt_contact_links(
    phone: str,
    telegram_username: str | None = None,
) -> str:
    """
    Build Telegram-native contact links.

    Priority:
    1. If telegram_username is set → t.me/{username}
    2. Fallback → tel: link only
    """
    parts = [fmt_phone_links(phone)]

    if telegram_username:
        clean = telegram_username.lstrip("@")
        parts.append(f'💬 <a href="https://t.me/{clean}">Написать в Telegram</a>')
    else:
        parts.append("<i>Telegram не указан</i>")

    return "\n".join(parts)


# ── Booking card ─────────────────────────────────────────────────────


def fmt_booking_compact(b: Booking) -> str:
    """One-line booking for daily summary / schedule lists."""
    icon = STATUS_EMOJI.get(b.status.value, "❓")
    name = b.student.full_name if b.student else "—"
    return f"{icon} 🕒 {fmt_time(b.appointment_time)} — {name} ({b.service_type})"


def fmt_booking_card(b: Booking) -> str:
    """Multi-line booking card."""
    icon = STATUS_EMOJI.get(b.status.value, "❓")
    name = b.student.full_name if b.student else "—"
    lines = [
        f"{icon} 🕒 <b>{fmt_time(b.appointment_time)}</b> — {name}",
        f"     {b.service_type}",
    ]
    return "\n".join(lines)


# ── Keyboard builders ────────────────────────────────────────────────


def build_booking_actions(b: Booking) -> list[InlineKeyboardButton]:
    """Inline action buttons for one booking based on its status.

    Uses time (HH:MM) as the human label instead of DB id.
    """
    from app.db.models import BookingStatus

    time_label = fmt_time(b.appointment_time)
    row: list[InlineKeyboardButton] = []
    if b.status == BookingStatus.PENDING:
        row.append(InlineKeyboardButton(
            text=f"✅ {time_label}", callback_data=f"confirm:{b.id}",
        ))
    if b.status in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
        row.append(InlineKeyboardButton(
            text=f"✖ {time_label}", callback_data=f"cancel:{b.id}",
        ))
    row.append(InlineKeyboardButton(
        text=f"📋 {time_label}", callback_data=f"detail:{b.id}",
    ))

    # Compact "Написать" button next to booking details
    if b.student and b.student.telegram_username:
        clean = b.student.telegram_username.lstrip("@")
        row.append(InlineKeyboardButton(
            text="💬", url=f"https://t.me/{clean}",
        ))

    return row


def build_page_nav(
    current: int,
    total_pages: int,
    prefix: str = "page",
) -> list[InlineKeyboardButton]:
    """Build [◀️] [1/3] [▶️] navigation row."""
    row: list[InlineKeyboardButton] = []

    if current > 0:
        row.append(InlineKeyboardButton(text="◀️", callback_data=f"{prefix}:{current - 1}"))
    else:
        row.append(InlineKeyboardButton(text="·", callback_data="noop"))

    row.append(InlineKeyboardButton(
        text=f"{current + 1}/{total_pages}", callback_data="noop",
    ))

    if current < total_pages - 1:
        row.append(InlineKeyboardButton(text="▶️", callback_data=f"{prefix}:{current + 1}"))
    else:
        row.append(InlineKeyboardButton(text="·", callback_data="noop"))

    return row
