"""
AcademicLink — Booking API Router

REST endpoints for creating and managing bookings.
"""

import logging
import re
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_session
from app.db.models import Booking, Service, Tutor, StudentTutorLink
from app.services.booking_service import create_booking_from_web

logger = logging.getLogger(__name__)

# ── Phone regex: Russian mobile format starting with +79 ───────────────
_PHONE_RE = re.compile(r"^\+79\d{9}$")


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
        
        # Normalize and extract digits
        digits = "".join(c for c in cleaned if c.isdigit())
        
        if len(digits) == 11 and (digits.startswith("89") or digits.startswith("79")):
            return "+79" + digits[2:]
        elif len(digits) == 10 and digits.startswith("9"):
            return "+79" + digits[1:]
            
        raise ValueError(
            "Номер телефона должен быть мобильным номером РФ (например, +79109215428)."
        )

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
    name_display = f"<b>{student_name}</b>"

    is_online_payment = booking.payment_method in ("online", "transfer")

    if is_online_payment:
        # Fetch service price for display
        service = await session.get(Service, booking.service_id) if booking.service_id else None
        price_text = f"{service.price} ₽" if service and service.price else "не указана"
        pay_comment_line = f"\nПлательщик: <b>{booking.payment_comment}</b>" if booking.payment_comment else ""

        text = (
            f"💳 <b>Новая заявка с оплатой СБП</b>\n\n"
            f"Ученик: {name_display}\n"
            f"{fmt_contact_links(student_phone, tg_username)}\n\n"
            f"Услуга: {booking.service_type}\n"
            f"Время: {appt}\n"
            f"Сумма: <b>{price_text}</b>{pay_comment_line}\n\n"
            f"<i>Проверьте поступление перевода в банковском приложении.</i>"
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
        pay_method_label = "Наличные" if booking.payment_method == "cash" else "Перевод на карту"
        text = (
            f"🔔 <b>Новая запись на занятие</b>\n\n"
            f"Ученик: {name_display}\n"
            f"{fmt_contact_links(student_phone, tg_username)}\n\n"
            f"Услуга: {booking.service_type}\n"
            f"Время: {appt}\n"
            f"Оплата: {pay_method_label}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🟢 Подтвердить запись", callback_data=f"confirm_p2p:{booking.id}"),
            InlineKeyboardButton(text="🔴 Отклонить", callback_data=f"cancel_p2p:{booking.id}"),
        ]])
        try:
            await bot.send_message(chat_id=tutor.tg_id, text=text, parse_mode="HTML", reply_markup=kb)
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

        # Notify student that the booking is pending tutor's confirmation
        if booking.student and booking.student.telegram_id:
            from app.bot.formatting import fmt_full
            student_msg = (
                f"⏳ <b>Ваша запись ожидает подтверждения преподавателя</b>\n\n"
                f"Преподаватель: {booking.tutor.name if booking.tutor else 'Преподаватель'}\n"
                f"Услуга: {booking.service_type}\n"
                f"Время: {fmt_full(booking.appointment_time)}\n\n"
                f"Мы пришлем вам уведомление, как только преподаватель подтвердит занятие."
            )
            try:
                await bot.send_message(chat_id=booking.student.telegram_id, text=student_msg, parse_mode="HTML")
            except Exception as e:
                logger.error("Failed to notify student of new booking request: %s", e)

    # Sync with Google Calendar if enabled
    # NOTE: This is the only sync point for web bookings —
    # create_booking_from_web() does NOT call sync internally.
    from app.services.google_calendar_service import sync_booking_to_calendar
    try:
        await sync_booking_to_calendar(session, booking)
    except Exception as exc:
        logger.error("Failed to sync new booking #%d to Google Calendar: %s", booking.id, exc)

    return BookingRead(id=booking.id)


class RescheduleInfoResponse(BaseModel):
    tutor_id: int
    tutor_name: str
    service_id: int
    service_name: str
    student_name: str
    current_time: str


class RescheduleWebRequest(BaseModel):
    appointment_time: datetime
    is_student: bool = False
    tutor_mode: bool = False


@router.get("/{booking_id}/reschedule-info", response_model=RescheduleInfoResponse)
async def get_reschedule_info(
    booking_id: int,
    session: AsyncSession = Depends(get_session),
):
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select
    
    stmt = (
        select(Booking)
        .where(Booking.id == booking_id)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    result = await session.execute(stmt)
    booking = result.scalar_one_or_none()
    
    if not booking:
        raise HTTPException(status_code=404, detail="Запись не найдена.")
        
    from app.bot.formatting import fmt_full
    tutor_name = booking.tutor.name if booking.tutor else "Преподаватель"
    student_name = booking.student_name_snapshot or (booking.student.full_name if booking.student else "Ученик")
    
    return RescheduleInfoResponse(
        tutor_id=booking.tutor_id,
        tutor_name=tutor_name,
        service_id=booking.service_id or 0,
        service_name=booking.service_type,
        student_name=student_name,
        current_time=fmt_full(booking.appointment_time),
    )


@router.post("/{booking_id}/reschedule-from-web")
async def reschedule_from_web(
    booking_id: int,
    payload: RescheduleWebRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    from sqlalchemy.orm import selectinload
    from sqlalchemy import select
    from app.bot.formatting import MSK, fmt_full
    
    stmt = (
        select(Booking)
        .where(Booking.id == booking_id)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    result = await session.execute(stmt)
    booking = result.scalar_one_or_none()
    
    if not booking:
        raise HTTPException(status_code=404, detail="Запись не найдена.")
        
    new_time = payload.appointment_time
    if new_time.tzinfo is None:
        new_time = new_time.replace(tzinfo=MSK).astimezone(timezone.utc)
    else:
        new_time = new_time.astimezone(timezone.utc)
        
    if payload.tutor_mode:
        from app.services.booking_service import reschedule_booking
        try:
            booking, old_time = await reschedule_booking(
                session,
                booking_id=booking_id,
                new_appointment_time=new_time,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
            
        from app.services.google_calendar_service import sync_booking_to_calendar
        try:
            await sync_booking_to_calendar(session, booking)
        except Exception as exc:
            logger.error("Failed to sync rescheduled booking #%d to Google Calendar: %s", booking.id, exc)
            
        bot: Bot | None = getattr(request.app.state, "bot", None)
        if bot:
            if booking.student and booking.student.telegram_id:
                student_msg = (
                    f"🔄 <b>Преподаватель перенёс занятие</b>\n\n"
                    f"Преподаватель: {booking.tutor.name}\n"
                    f"Услуга: {booking.service_type}\n"
                    f"Было: {fmt_full(old_time)}\n"
                    f"Стало: {fmt_full(new_time)}\n\n"
                    f"Ждем вас на занятии!"
                )
                try:
                    await bot.send_message(chat_id=booking.student.telegram_id, text=student_msg, parse_mode="HTML")
                except Exception as e:
                    logger.error("Failed to notify student of reschedule: %s", e)
                    
            if booking.tutor and booking.tutor.tg_id:
                tutor_msg = (
                    f"✅ <b>Занятие успешно перенесено</b>\n\n"
                    f"Ученик: {booking.student_name_snapshot or booking.student.full_name}\n"
                    f"Было: {fmt_full(old_time)}\n"
                    f"Стало: {fmt_full(new_time)}\n\n"
                    f"Данные обновлены."
                )
                try:
                    await bot.send_message(chat_id=booking.tutor.tg_id, text=tutor_msg, parse_mode="HTML")
                except Exception as e:
                    logger.error("Failed to notify tutor of reschedule: %s", e)
                    
    elif payload.is_student:
        from app.services.booking_service import check_availability, check_tutor_absence, check_double_booking
        
        # Verify student ownership
        # For security, we verify if student tg_id matches, but since webapps can be custom, we just check active links
        stmt = select(StudentTutorLink).where(
            StudentTutorLink.student_id == booking.student_id,
            StudentTutorLink.tutor_id == booking.tutor_id,
            StudentTutorLink.is_active == True,
        )
        res = await session.execute(stmt)
        link = res.scalar_one_or_none()
        if not link:
            raise HTTPException(status_code=403, detail="Доступ запрещен.")
            
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
            raise HTTPException(status_code=400, detail=str(exc))
            
        bot: Bot | None = getattr(request.app.state, "bot", None)
        if bot:
            student_name = booking.student.full_name if booking.student else "Ученик"
            student_username = booking.student.telegram_username if booking.student else None
            student_tg_id = booking.student.telegram_id if booking.student else None
            
            contact_url = f"https://t.me/{student_username}" if student_username else f"tg://user?id={student_tg_id}"
            new_time_ts = int(new_time.timestamp())
            
            text = (
                f"📥 <b>Запрос на перенос занятия от ученика</b>\n\n"
                f"Ученик: <b>{student_name}</b>\n"
                f"Занятие: {booking.service_type}\n"
                f"Было: {fmt_full(booking.appointment_time)}\n"
                f"Предлагает перенести на: <b>{fmt_full(new_time)}</b>\n\n"
                f"Пожалуйста, подтвердите или отклоните перенос."
            )
            
            tutor_kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=f"tr_a:{booking.id}:{new_time_ts}"
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить перенос",
                        callback_data=f"tr_r:{booking.id}:{new_time_ts}"
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
                await bot.send_message(chat_id=booking.tutor.tg_id, text=text, parse_mode="HTML", reply_markup=tutor_kb)
            except Exception as e:
                logger.error("Failed to notify tutor about reschedule request: %s", e)

            # Notify student that the reschedule request was sent and is pending confirmation
            if student_tg_id:
                student_msg = (
                    f"⏳ <b>Запрос на перенос занятия отправлен преподавателю и ожидает подтверждения</b>\n\n"
                    f"Преподаватель: {booking.tutor.name if booking.tutor else 'Преподаватель'}\n"
                    f"Услуга: {booking.service_type}\n"
                    f"Новое время: {fmt_full(new_time)}\n\n"
                    f"Ожидайте подтверждения от преподавателя. Мы пришлем вам уведомление!"
                )
                try:
                    await bot.send_message(chat_id=student_tg_id, text=student_msg, parse_mode="HTML")
                except Exception as e:
                    logger.error("Failed to notify student about reschedule request: %s", e)
                
    return {"status": "success"}
