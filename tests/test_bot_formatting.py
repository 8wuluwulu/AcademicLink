"""
AcademicLink — Tests for Bot Formatting Utilities
"""

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock
import pytest
from aiogram.types import InlineKeyboardButton

from app.bot.formatting import (
    MSK,
    fmt_date,
    fmt_date_short,
    fmt_date_dot,
    fmt_time,
    fmt_full,
    fmt_month_year,
    fmt_phone_links,
    fmt_contact_links,
    fmt_booking_compact,
    fmt_booking_card,
    build_booking_actions,
    build_page_nav,
)
from app.db.models import Booking, BookingStatus, Student


def test_time_formatting():
    # 2026-06-18 10:00 UTC -> 2026-06-18 13:00 MSK
    dt = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    
    assert fmt_time(dt) == "13:00"
    assert fmt_date_dot(dt) == "18.06.2026"
    assert fmt_full(dt) == "18.06.2026 13:00"
    
    # Thursday in Russian is "Четверг", month is "июня"
    assert fmt_date(dt) == "18 июня 2026, Четверг"
    assert fmt_date_short(dt) == "18 июня, Чт"
    assert fmt_month_year(dt) == "Июнь 2026"


def test_contact_links():
    phone = "+79001234567"
    assert fmt_phone_links(phone) == 'Телефон: <a href="tel:+79001234567">+79001234567</a>'

    # Without telegram username
    links_no_tg = fmt_contact_links(phone, None)
    assert phone in links_no_tg
    assert "Telegram: <i>не указан</i>" in links_no_tg

    # With telegram username (with @ prefix)
    links_with_tg1 = fmt_contact_links(phone, "@ivan")
    assert "https://t.me/ivan" in links_with_tg1
    assert "@ivan" in links_with_tg1

    # With telegram username (without @ prefix)
    links_with_tg2 = fmt_contact_links(phone, "ivan")
    assert "https://t.me/ivan" in links_with_tg2
    assert "@ivan" in links_with_tg2


def test_booking_compact_and_card():
    student = MagicMock(spec=Student)
    student.full_name = "Иванов Иван"

    booking = MagicMock(spec=Booking)
    booking.status = BookingStatus.CONFIRMED
    booking.appointment_time = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    booking.student = student
    booking.service_type = "Математика"

    compact = fmt_booking_compact(booking)
    assert "🟢" in compact
    assert "13:00" in compact
    assert "Иванов Иван" in compact
    assert "Математика" in compact

    card = fmt_booking_card(booking)
    assert "🟢" in card
    assert "13:00" in card
    assert "Иванов Иван" in card
    assert "Математика" in card

    # No student fallback
    booking.student = None
    compact_no_student = fmt_booking_compact(booking)
    assert "—" in compact_no_student


def test_build_booking_actions():
    student_with_username = MagicMock(spec=Student)
    student_with_username.telegram_username = "@ivan_tg"
    
    booking_pending = MagicMock(spec=Booking)
    booking_pending.id = 42
    booking_pending.status = BookingStatus.PENDING
    booking_pending.appointment_time = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    booking_pending.student = student_with_username

    actions_pending = build_booking_actions(booking_pending)
    
    # Should contain Confirm (✅), Cancel (✖), Details (📋) and Message (💬)
    assert len(actions_pending) == 4
    assert actions_pending[0].text == "✅ 13:00"
    assert actions_pending[0].callback_data == "confirm:42"
    assert actions_pending[1].text == "✖ 13:00"
    assert actions_pending[1].callback_data == "cancel:42"
    assert actions_pending[2].text == "📋 13:00"
    assert actions_pending[2].callback_data == "detail:42"
    assert actions_pending[3].text == "💬"
    assert actions_pending[3].url == "https://t.me/ivan_tg"

    # Confirmed booking (no confirm button, but still cancel and detail)
    booking_confirmed = MagicMock(spec=Booking)
    booking_confirmed.id = 43
    booking_confirmed.status = BookingStatus.CONFIRMED
    booking_confirmed.appointment_time = datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc)
    booking_confirmed.student = None  # No student telegram
    
    actions_confirmed = build_booking_actions(booking_confirmed)
    assert len(actions_confirmed) == 2
    assert actions_confirmed[0].text == "✖ 13:00"
    assert actions_confirmed[1].text == "📋 13:00"


def test_build_page_nav():
    # Current page 0 of 3
    nav_first = build_page_nav(0, 3)
    assert len(nav_first) == 3
    assert nav_first[0].text == "·"
    assert nav_first[0].callback_data == "noop"
    assert nav_first[1].text == "1/3"
    assert nav_first[2].text == "▶️"
    assert nav_first[2].callback_data == "page:1"

    # Current page 1 of 3
    nav_mid = build_page_nav(1, 3, prefix="testpage")
    assert len(nav_mid) == 3
    assert nav_mid[0].text == "◀️"
    assert nav_mid[0].callback_data == "testpage:0"
    assert nav_mid[1].text == "2/3"
    assert nav_mid[2].text == "▶️"
    assert nav_mid[2].callback_data == "testpage:2"

    # Current page 2 of 3
    nav_last = build_page_nav(2, 3)
    assert len(nav_last) == 3
    assert nav_last[0].text == "◀️"
    assert nav_last[1].text == "3/3"
    assert nav_last[2].text == "·"
