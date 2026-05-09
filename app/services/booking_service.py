"""
AcademicLink — Booking Service

Core business logic for creating and managing bookings.
All functions receive an ``AsyncSession`` so they can be called
from both the FastAPI API layer and the Telegram bot layer.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Booking, BookingStatus, Student, Tutor

logger = logging.getLogger(__name__)


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
    3. Create a ``Booking`` with ``PENDING`` status.
    4. Commit and return the booking with relationships loaded.

    Raises
    ------
    ValueError
        If the requested tutor is not found, is inactive, or no active
        tutor exists in the database.
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

    # ── 3. Create booking ────────────────────────────────────────────
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
