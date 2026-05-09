"""
AcademicLink — Booking API Router

REST endpoints for creating and managing bookings.
Each request is scoped to a specific ``tutor_id`` (multi-tenant model).
"""

import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import Booking, Tutor
from app.services.booking_service import create_booking_from_web

logger = logging.getLogger(__name__)

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
    phone: str = Field(
        ...,
        min_length=7,
        max_length=20,
        description="Student's contact phone number",
        examples=["+998901234567"],
    )
    service_type: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Type of tutoring service requested",
        examples=["IELTS Preparation"],
    )
    appointment_time: datetime = Field(
        ...,
        description="Desired appointment date and time",
        examples=["2026-05-15T14:00:00"],
    )
    tutor_id: int = Field(
        ...,
        gt=0,
        description="ID of the tutor to book a session with",
        examples=[1],
    )
    telegram_username: str | None = Field(
        default=None,
        max_length=32,
        description="Student's Telegram @username (optional)",
        examples=["johndoe"],
    )


class BookingRead(BaseModel):
    """Response body returned after a booking is created."""

    id: int = Field(..., description="Unique booking identifier")
    status: str = Field(
        default="success",
        description="Operation result status",
    )

    model_config = {"from_attributes": True}


# ── Tutor Notification ───────────────────────────────────────────────


async def notify_tutor_new_booking(
    booking: Booking,
    session: AsyncSession,
    bot: Bot,
) -> None:
    """
    Send a Telegram notification to the tutor about a new booking.

    Accepts an explicit ``Bot`` instance so the caller (route handler)
    controls where the bot reference comes from (``request.app.state.bot``).

    Gracefully degrades on any Telegram / network error — a notification
    failure must never break the booking flow.
    """
    # Fetch tutor to get their Telegram ID
    tutor = await session.get(Tutor, booking.tutor_id)
    if tutor is None:
        logger.warning(
            "Tutor id=%d not found — cannot send notification", booking.tutor_id
        )
        return

    # Use eagerly loaded relationship for student data
    student_name = "Неизвестно"
    student_phone = "—"
    tg_username = None
    if booking.student is not None:
        student_name = booking.student.full_name
        student_phone = booking.student.phone
        tg_username = booking.student.telegram_username

    from app.bot.formatting import fmt_contact_links, fmt_full

    appt = fmt_full(booking.appointment_time)

    text = (
        f"🔔 <b>Новая запись!</b>\n\n"
        f"👤 <b>{student_name}</b>\n"
        f"{fmt_contact_links(student_phone, tg_username)}\n\n"
        f"{booking.service_type}\n"
        f"🕒 {appt}"
    )

    try:
        await bot.send_message(chat_id=tutor.tg_id, text=text, parse_mode="HTML")
        logger.info(
            "Notification sent to tutor tg_id=%d for booking #%d",
            tutor.tg_id,
            booking.id,
        )
    except TelegramForbiddenError:
        logger.error(
            "Tutor tg_id=%d has blocked the bot — cannot deliver booking #%d",
            tutor.tg_id,
            booking.id,
        )
    except Exception as exc:
        logger.error(
            "Failed to notify tutor tg_id=%d for booking #%d: %s",
            tutor.tg_id,
            booking.id,
            exc,
        )


# ── Router ───────────────────────────────────────────────────────────

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.post(
    "/",
    response_model=BookingRead,
    status_code=201,
    summary="Create a new booking",
    description="Submit a booking request for a specific tutor.",
)
async def create_booking(
    payload: BookingCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> BookingRead:
    """
    Create a booking for a student with a specific tutor.

    The endpoint validates the request body, delegates to the booking
    service, triggers a Telegram notification, and returns the
    created booking ID.
    """
    try:
        booking = await create_booking_from_web(
            session,
            full_name=payload.full_name,
            phone=payload.phone,
            service_type=payload.service_type,
            appointment_time=payload.appointment_time,
            tutor_id=payload.tutor_id,
            telegram_username=payload.telegram_username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Extract bot from application state and send notification
    bot: Bot | None = getattr(request.app.state, "bot", None)
    if bot is not None:
        await notify_tutor_new_booking(booking, session, bot)
    else:
        logger.warning(
            "Bot not initialised — skipping notification for booking #%d",
            booking.id,
        )

    return BookingRead(id=booking.id, status="success")
