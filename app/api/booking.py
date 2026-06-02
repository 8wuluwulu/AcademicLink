"""
AcademicLink — Booking API Router

REST endpoints for creating and managing bookings.
"""

import logging
import re
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_session
from app.db.models import Booking, Service, Tutor
from app.services.booking_service import create_booking_from_web

logger = logging.getLogger(__name__)

# ── Phone regex: international format starting with + ────────────────
_PHONE_RE = re.compile(r"^\+\d{10,15}$")


# ── Pydantic Schemas ─────────────────────────────────────────────────


class BookingCreate(BaseModel):
    """Request body for creating a new booking."""

    full_name: str = Field(
        ...,
        min_length=2,
        max_length=255,
        description="Student's full name",
        examples=["John Doe"],
    )
    phone: str | None = Field(
        default=None,
        min_length=7,
        max_length=20,
        description="Contact phone number (optional)",
        examples=["+998901234567"],
    )
    service_id: int = Field(
        ...,
        gt=0,
        description="ID of the service to book",
        examples=[1],
    )
    appointment_time: datetime = Field(
        ...,
        description="Desired appointment date and time",
        examples=["2026-05-15T14:00:00"],
    )
    tutor_id: int = Field(
        ...,
        gt=0,
        description="ID of the tutor",
        examples=[1],
    )
    telegram_username: str | None = Field(
        default=None,
        max_length=32,
        description="Telegram @username (optional)",
        examples=["johndoe"],
    )
    telegram_id: int | None = Field(
        default=None,
        description="Telegram user ID (optional)",
        examples=[123456789],
    )
    payment_method: str = Field(
        default="cash",
        description="Payment method: cash, transfer, or online",
        examples=["cash"],
    )
    payment_comment: str | None = Field(
        default=None,
        max_length=500,
        description="Payer verification info for SBP transfers (e.g. sender's name)",
        examples=["Иванов Иван"],
    )

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = v.strip()
        if not _PHONE_RE.match(cleaned):
            raise ValueError(
                "Номер телефона должен быть в международном формате (+7...)."
            )
        return cleaned

    @field_validator("payment_method")
    @classmethod
    def validate_payment_method(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if cleaned not in ("cash", "transfer", "online"):
            raise ValueError("Способ оплаты должен быть 'cash', 'transfer' или 'online'.")
        return cleaned


class BookingRead(BaseModel):
    id: int
    status: str = "success"

    model_config = {"from_attributes": True}


# ── Security Dependency ──────────────────────────────────────────────


async def verify_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> str:
    if x_api_key != settings.secret_key:
        raise HTTPException(status_code=403, detail="Invalid API key.")
    return x_api_key


# ── Tutor Notification ───────────────────────────────────────────────


async def notify_tutor_new_booking(
    booking: Booking,
    session: AsyncSession,
    bot: Bot,
) -> None:
    tutor = await session.get(Tutor, booking.tutor_id)
    if tutor is None:
        return

    student_name = booking.student_name_snapshot or (booking.student.full_name if booking.student else "Неизвестно")
    student_phone = booking.student.phone if booking.student else "—"
    tg_username = booking.student.telegram_username if booking.student else None

    from app.bot.formatting import fmt_contact_links, fmt_full

    appt = fmt_full(booking.appointment_time)
    name_display = f"👤 <b>{student_name}</b>"

    is_online_payment = booking.payment_method in ("online", "transfer")

    if is_online_payment:
        # Fetch service price for display
        service = await session.get(Service, booking.service_id) if booking.service_id else None
        price_text = f"{service.price} ₽" if service and service.price else "не указана"
        pay_comment_line = f"\n👤 Плательщик: <b>{booking.payment_comment}</b>" if booking.payment_comment else ""

        text = (
            f"💳 <b>Новая заявка с оплатой СБП!</b>\n\n"
            f"{name_display}\n"
            f"{fmt_contact_links(student_phone, tg_username)}\n\n"
            f"📚 {booking.service_type}\n"
            f"🕒 {appt}\n"
            f"💰 Сумма: <b>{price_text}</b>{pay_comment_line}\n\n"
            f"⚠️ <i>Проверьте поступление перевода в вашем банковском приложении!</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🟢 Подтвердить оплату", callback_data=f"confirm_p2p:{booking.id}"),
            InlineKeyboardButton(text="🔴 Отклонить", callback_data=f"cancel_p2p:{booking.id}"),
        ]])
        try:
            await bot.send_message(chat_id=tutor.tg_id, text=text, parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            logger.error("Failed to notify tutor about P2P booking: %s", exc)
    else:
        pay_method_label = "💵 Наличные" if booking.payment_method == "cash" else "💳 Перевод на карту"
        text = (
            f"🔔 <b>Новая запись!</b>\n\n"
            f"{name_display}\n"
            f"{fmt_contact_links(student_phone, tg_username)}\n\n"
            f"📚 {booking.service_type}\n"
            f"🕒 {appt}\n"
            f"💰 Оплата: {pay_method_label}"
        )
        try:
            await bot.send_message(chat_id=tutor.tg_id, text=text, parse_mode="HTML")
        except Exception as exc:
            logger.error("Failed to notify tutor: %s", exc)


# ── Router ───────────────────────────────────────────────────────────

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.post("/", response_model=BookingRead, status_code=201)
async def create_booking(
    payload: BookingCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> BookingRead:
    try:
        booking = await create_booking_from_web(
            session,
            full_name=payload.full_name,
            phone=payload.phone,
            service_id=payload.service_id,
            appointment_time=payload.appointment_time,
            tutor_id=payload.tutor_id,
            telegram_username=payload.telegram_username,
            telegram_id=payload.telegram_id,
            payment_method=payload.payment_method,
            payment_comment=payload.payment_comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    bot: Bot | None = getattr(request.app.state, "bot", None)
    if bot:
        await notify_tutor_new_booking(booking, session, bot)

    # Sync with Google Calendar if enabled
    from app.services.google_calendar_service import sync_booking_to_calendar
    try:
        await sync_booking_to_calendar(session, booking)
    except Exception as exc:
        logger.error("Failed to sync new booking #%d to Google Calendar: %s", booking.id, exc)

    return BookingRead(id=booking.id)
