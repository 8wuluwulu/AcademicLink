"""
AcademicLink — Booking Service

Core business logic for creating and managing bookings.
All functions receive an ``AsyncSession`` so they can be called
from both the FastAPI API layer and the Telegram bot layer.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AvailabilitySlot, Booking, BookingStatus, Student, Tutor

logger = logging.getLogger(__name__)

# ── Validation Helpers ───────────────────────────────────────────────

LESSON_DURATION_MINUTES = 60  # default lesson window for overlap check


async def check_availability(
    session: AsyncSession,
    *,
    tutor_id: int,
    appointment_time: datetime,
) -> None:
    """
    Verify that ``appointment_time`` falls within one of the tutor's
    :class:`AvailabilitySlot` entries.

    Checks are based on the **weekday** (0=Monday … 6=Sunday) and
    the slot's ``start_time`` / ``end_time`` boundaries.

    Raises
    ------
    ValueError
        If the tutor has no availability slot covering the requested time.
    """
    weekday = appointment_time.weekday()  # 0-6
    appt_time = appointment_time.time()

    stmt = select(AvailabilitySlot).where(
        AvailabilitySlot.tutor_id == tutor_id,
        AvailabilitySlot.weekday == weekday,
        AvailabilitySlot.start_time <= appt_time,
        AvailabilitySlot.end_time > appt_time,
    )
    result = await session.execute(stmt)
    slot = result.scalar_one_or_none()

    if slot is None:
        day_names = [
            "Понедельник", "Вторник", "Среда", "Четверг",
            "Пятница", "Суббота", "Воскресенье",
        ]
        raise ValueError(
            f"Репетитор не принимает в это время. "
            f"{day_names[weekday]} {appt_time:%H:%M} не входит "
            f"ни в один слот доступности."
        )


async def check_double_booking(
    session: AsyncSession,
    *,
    tutor_id: int,
    appointment_time: datetime,
) -> None:
    """
    Ensure no CONFIRMED booking exists for the same tutor within
    a ±60-minute window around ``appointment_time``.

    Raises
    ------
    ValueError
        If an overlapping confirmed booking is found.
    """
    window_start = appointment_time - timedelta(minutes=LESSON_DURATION_MINUTES)
    window_end = appointment_time + timedelta(minutes=LESSON_DURATION_MINUTES)

    stmt = select(Booking).where(
        Booking.tutor_id == tutor_id,
        Booking.status == BookingStatus.CONFIRMED,
        Booking.appointment_time >= window_start,
        Booking.appointment_time < window_end,
    )
    result = await session.execute(stmt)
    conflict = result.scalar_one_or_none()

    if conflict is not None:
        raise ValueError(
            f"Временной конфликт: у репетитора уже есть подтверждённое "
            f"занятие в {conflict.appointment_time:%d.%m.%Y %H:%M}. "
            f"Выберите другое время (минимум 60 минут между занятиями)."
        )


# ── Main Service Function ───────────────────────────────────────────


async def create_booking_from_web(
    session: AsyncSession,
    *,
    full_name: str,
    phone: str,
    service_type: str,
    appointment_time: datetime,
    tutor_id: int | None = None,
    telegram_id: int | None = None,
    telegram_username: str | None = None,
) -> Booking:
    """
    Create a new booking from a web-form submission.

    Flow
    ----
    1. Look up an existing ``Student`` by *phone* (unique).
       → If not found, create one.
    2. Resolve the target ``Tutor``:
       - If *tutor_id* is provided (multi-tenant), look up that specific tutor.
       - Otherwise, pick the first active tutor (single-tutor MVP fallback).
    3. **Availability check**: verify the time falls within a tutor's slot.
    4. **Overlap check**: ensure no confirmed booking within ±60 min.
    5. Create a ``Booking`` with ``PENDING`` status.
    6. Commit and return the booking with relationships loaded.

    Raises
    ------
    ValueError
        If the requested tutor is not found, is inactive, no active
        tutor exists, the slot is unavailable, or there is a time conflict.
    """
    # ── 0. Normalize appointment_time to UTC-aware ────────────────────
    if appointment_time.tzinfo is None:
        appointment_time = appointment_time.replace(tzinfo=timezone.utc)

    # ── 1. Resolve student ───────────────────────────────────────────
    stmt = select(Student).where(Student.phone == phone)
    result = await session.execute(stmt)
    student = result.scalar_one_or_none()

    if student is None:
        student = Student(
            full_name=full_name,
            phone=phone,
            telegram_id=telegram_id,
            telegram_username=telegram_username.lstrip("@") if telegram_username else None,
        )
        session.add(student)
        await session.flush()  # populate student.id
        logger.info("Created new student: %s (phone=%s)", full_name, phone)
    else:
        # Reactivate student if they were archived
        if not student.is_active:
            student.is_active = True
            logger.info("Reactivated student id=%d (phone=%s)", student.id, phone)

        # Update name / telegram fields if provided
        if student.full_name != full_name:
            student.full_name = full_name
        if telegram_id is not None and student.telegram_id != telegram_id:
            student.telegram_id = telegram_id
        if telegram_username is not None:
            clean_username = telegram_username.lstrip("@")
            if student.telegram_username != clean_username:
                student.telegram_username = clean_username
        logger.info("Found existing student id=%d for phone=%s", student.id, phone)

    # ── 2. Resolve tutor ─────────────────────────────────────────────
    if tutor_id is not None:
        # Multi-tenant: look up the specific tutor
        stmt = select(Tutor).where(Tutor.id == tutor_id)
        result = await session.execute(stmt)
        tutor = result.scalar_one_or_none()

        if tutor is None:
            raise ValueError(f"Tutor with id={tutor_id} not found.")
        if not tutor.is_active:
            raise ValueError(
                f"Tutor '{tutor.name}' (id={tutor_id}) is not currently "
                "accepting bookings."
            )
    else:
        # Single-tutor MVP fallback: pick the first active tutor
        stmt = select(Tutor).where(Tutor.is_active.is_(True)).limit(1)
        result = await session.execute(stmt)
        tutor = result.scalar_one_or_none()

        if tutor is None:
            raise ValueError(
                "No active tutor found. "
                "Run ensure_tutor_exists() during startup to seed a default tutor."
            )

    # ── 3. Availability check ────────────────────────────────────────
    await check_availability(
        session, tutor_id=tutor.id, appointment_time=appointment_time,
    )

    # ── 4. Double-booking / overlap check ────────────────────────────
    await check_double_booking(
        session, tutor_id=tutor.id, appointment_time=appointment_time,
    )

    # ── 5. Create booking ────────────────────────────────────────────
    booking = Booking(
        student_id=student.id,
        tutor_id=tutor.id,
        service_type=service_type,
        appointment_time=appointment_time,
        status=BookingStatus.PENDING,
        created_at=datetime.now(timezone.utc),
    )
    session.add(booking)
    await session.commit()

    # Re-fetch with relationships eagerly loaded so callers
    # (e.g. notify_tutor_new_booking) can access booking.student / .tutor
    stmt = (
        select(Booking)
        .where(Booking.id == booking.id)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    result = await session.execute(stmt)
    booking = result.scalar_one()

    logger.info(
        "Booking #%d created — student=%d tutor=%d service=%r",
        booking.id,
        booking.student_id,
        booking.tutor_id,
        booking.service_type,
    )
    return booking
