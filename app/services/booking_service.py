"""
AcademicLink — Booking Service

Core business logic for creating and managing bookings.
All functions receive an ``AsyncSession`` so they can be called
from both the FastAPI API layer and the Telegram bot layer.
"""

import logging
from datetime import datetime

from sqlalchemy import select
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
    telegram_id: int | None = None,
) -> Booking:
    """
    Create a new booking from a web-form submission.

    Flow
    ----
    1. Look up an existing ``Student`` by *phone* (unique).
       → If not found, create one.
    2. Pick the first *active* ``Tutor`` (single-tutor MVP).
    3. Create a ``Booking`` with ``PENDING`` status.
    4. Commit and return the booking with relationships loaded.

    Raises
    ------
    ValueError
        If no active tutor exists in the database.
    """
    # ── 1. Resolve student ───────────────────────────────────────────
    stmt = select(Student).where(Student.phone == phone)
    result = await session.execute(stmt)
    student = result.scalar_one_or_none()

    if student is None:
        student = Student(
            full_name=full_name,
            phone=phone,
            telegram_id=telegram_id,
        )
        session.add(student)
        await session.flush()  # populate student.id
        logger.info("Created new student: %s (phone=%s)", full_name, phone)
    else:
        # Update name / telegram_id if provided
        if student.full_name != full_name:
            student.full_name = full_name
        if telegram_id is not None and student.telegram_id != telegram_id:
            student.telegram_id = telegram_id
        logger.info("Found existing student id=%d for phone=%s", student.id, phone)

    # ── 2. Get active tutor ──────────────────────────────────────────
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
    )
    session.add(booking)
    await session.commit()
    await session.refresh(booking)

    logger.info(
        "Booking #%d created — student=%d tutor=%d service=%r",
        booking.id,
        booking.student_id,
        booking.tutor_id,
        booking.service_type,
    )
    return booking
