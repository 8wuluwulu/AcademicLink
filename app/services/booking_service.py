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

from app.db.models import AvailabilitySlot, Booking, BookingStatus, Student, Tutor, TutorAbsence

logger = logging.getLogger(__name__)

# ── Validation Helpers ───────────────────────────────────────────────

DEFAULT_LESSON_DURATION = 60  # fallback if tutor has no custom setting


async def check_tutor_absence(
    session: AsyncSession,
    *,
    tutor_id: int,
    appointment_time: datetime,
) -> None:
    """
    Ensure the requested ``appointment_time`` does not overlap
    with a :class:`TutorAbsence`.

    Raises
    ------
    ValueError
        If the tutor is on absence during the requested time.
    """
    stmt = select(TutorAbsence).where(
        TutorAbsence.tutor_id == tutor_id,
        TutorAbsence.start_time <= appointment_time,
        TutorAbsence.end_time > appointment_time,
    )
    result = await session.execute(stmt)
    absence = result.scalar_one_or_none()

    if absence is not None:
        reason_text = f" ({absence.reason})" if absence.reason else ""
        raise ValueError(
            f"Репетитор отсутствует в это время{reason_text}. "
            f"Выберите другую дату."
        )


async def check_availability(
    session: AsyncSession,
    *,
    tutor_id: int,
    appointment_time: datetime,
) -> None:
    """
    Ensure the requested ``appointment_time`` falls within one of the
    tutor's :class:`AvailabilitySlot` windows for that weekday.

    The appointment time is converted to MSK (the tutor's local timezone)
    before comparing against slot boundaries.

    Raises
    ------
    ValueError
        If the tutor has no slots for the day or the time is outside
        all defined windows.
    """
    from app.bot.formatting import MSK

    # Use MSK for validation to match the tutor's local context
    local_time = appointment_time.astimezone(MSK)
    weekday = local_time.weekday()
    local_clock = local_time.time()

    # Query AvailabilitySlot for this tutor + weekday
    stmt = select(AvailabilitySlot).where(
        AvailabilitySlot.tutor_id == tutor_id,
        AvailabilitySlot.weekday == weekday,
    )
    result = await session.execute(stmt)
    slots = result.scalars().all()

    if not slots:
        raise ValueError(
            "Репетитор не принимает в этот день. Выберите другой день."
        )

    # Check if time falls within ANY slot for that day
    for slot in slots:
        if slot.start_time <= local_clock < slot.end_time:
            return  # valid

    windows = ", ".join(
        f"{s.start_time:%H:%M}–{s.end_time:%H:%M}" for s in slots
    )
    raise ValueError(
        f"Репетитор не принимает в {local_clock:%H:%M}. "
        f"Доступные окна: {windows}."
    )


async def check_double_booking(
    session: AsyncSession,
    *,
    tutor_id: int,
    appointment_time: datetime,
    lesson_duration: int = DEFAULT_LESSON_DURATION,
    buffer_time: int = 0,
    exclude_booking_id: int | None = None,
) -> None:
    """
    Ensure no CONFIRMED or PENDING booking exists for the same tutor
    within a window calculated from the tutor's ``lesson_duration``
    plus ``buffer_time``.

    Checking both statuses prevents the "race condition" where multiple
    students could book the same slot while all bookings are still
    PENDING.  This enforces first-come, first-served semantics.

    Parameters
    ----------
    exclude_booking_id
        If provided, skip this booking ID from the overlap check
        (used by reschedule to avoid self-conflict).

    Raises
    ------
    ValueError
        If an overlapping active booking is found.
    """
    total_block = lesson_duration + buffer_time
    window_start = appointment_time - timedelta(minutes=total_block)
    window_end = appointment_time + timedelta(minutes=total_block)

    stmt = select(Booking).where(
        Booking.tutor_id == tutor_id,
        Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.PENDING]),
        Booking.appointment_time >= window_start,
        Booking.appointment_time < window_end,
    )
    if exclude_booking_id is not None:
        stmt = stmt.where(Booking.id != exclude_booking_id)
    result = await session.execute(stmt)
    conflict = result.scalar_one_or_none()

    if conflict is not None:
        raise ValueError(
            f"Временной конфликт: у репетитора уже есть занятие "
            f"в {conflict.appointment_time:%d.%m.%Y %H:%M}. "
            f"Выберите другое время (минимум {total_block} минут между занятиями)."
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

    # ── 4. Absence check ─────────────────────────────────────────────
    await check_tutor_absence(
        session, tutor_id=tutor.id, appointment_time=appointment_time,
    )

    # ── 5. Double-booking / overlap check ────────────────────────────
    await check_double_booking(
        session,
        tutor_id=tutor.id,
        appointment_time=appointment_time,
        lesson_duration=tutor.lesson_duration,
        buffer_time=tutor.buffer_time,
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


async def reschedule_booking(
    session: AsyncSession,
    *,
    booking_id: int,
    new_appointment_time: datetime,
) -> tuple[Booking, datetime]:
    """
    Reschedule an existing booking to a new date/time.

    Re-runs all validation checks (availability, absence, double-booking)
    against the new time.  Returns the updated booking **and** the old
    appointment time so the caller can build a notification message.

    Raises
    ------
    ValueError
        If the booking is not found, already processed, or the new
        time fails any validation check.
    """
    # ── 0. Normalize to UTC ───────────────────────────────────────────
    if new_appointment_time.tzinfo is None:
        new_appointment_time = new_appointment_time.replace(tzinfo=timezone.utc)

    # ── 1. Fetch booking ──────────────────────────────────────────────
    stmt = (
        select(Booking)
        .where(Booking.id == booking_id)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    result = await session.execute(stmt)
    booking = result.scalar_one_or_none()

    if booking is None:
        raise ValueError("Запись не найдена.")
    if booking.status not in (BookingStatus.PENDING, BookingStatus.CONFIRMED):
        raise ValueError(
            f"Нельзя перенести запись со статусом «{booking.status.value}»."
        )

    tutor = booking.tutor
    if tutor is None:
        raise ValueError("Репетитор не найден.")

    # ── 2. Run validations against the NEW time ───────────────────────
    await check_availability(
        session, tutor_id=tutor.id, appointment_time=new_appointment_time,
    )
    await check_tutor_absence(
        session, tutor_id=tutor.id, appointment_time=new_appointment_time,
    )
    await check_double_booking(
        session,
        tutor_id=tutor.id,
        appointment_time=new_appointment_time,
        lesson_duration=tutor.lesson_duration,
        buffer_time=tutor.buffer_time,
        exclude_booking_id=booking.id,
    )

    # ── 3. Update booking ─────────────────────────────────────────────
    old_time = booking.appointment_time
    booking.appointment_time = new_appointment_time
    booking.status = BookingStatus.CONFIRMED
    await session.commit()

    # Refresh with relationships
    await session.refresh(booking, attribute_names=["student", "tutor"])

    logger.info(
        "Booking #%d rescheduled from %s to %s",
        booking.id, old_time, new_appointment_time,
    )
    return booking, old_time


async def create_booking_internal(
    session: AsyncSession,
    *,
    student_id: int,
    tutor_id: int,
    service_type: str,
    appointment_time: datetime,
) -> Booking:
    """
    Create a CONFIRMED booking initiated by the tutor (internal CRM entry).

    Unlike :func:`create_booking_from_web`, this skips student
    resolution (the student already exists) and creates the booking
    as ``CONFIRMED`` immediately.

    Raises
    ------
    ValueError
        If the student/tutor is not found, is inactive, or the
        time fails any validation check.
    """
    # ── 0. Normalize to UTC ───────────────────────────────────────────
    if appointment_time.tzinfo is None:
        appointment_time = appointment_time.replace(tzinfo=timezone.utc)

    # ── 1. Resolve student ────────────────────────────────────────────
    student = await session.get(Student, student_id)
    if student is None:
        raise ValueError("Ученик не найден.")
    if not student.is_active:
        raise ValueError(f"Ученик «{student.full_name}» архивирован.")

    # ── 2. Resolve tutor ──────────────────────────────────────────────
    tutor = await session.get(Tutor, tutor_id)
    if tutor is None:
        raise ValueError("Репетитор не найден.")
    if not tutor.is_active:
        raise ValueError("Репетитор не принимает записи.")

    # ── 3. Validations ────────────────────────────────────────────────
    await check_availability(
        session, tutor_id=tutor.id, appointment_time=appointment_time,
    )
    await check_tutor_absence(
        session, tutor_id=tutor.id, appointment_time=appointment_time,
    )
    await check_double_booking(
        session,
        tutor_id=tutor.id,
        appointment_time=appointment_time,
        lesson_duration=tutor.lesson_duration,
        buffer_time=tutor.buffer_time,
    )

    # ── 4. Create booking (CONFIRMED — tutor-initiated) ───────────────
    booking = Booking(
        student_id=student.id,
        tutor_id=tutor.id,
        service_type=service_type,
        appointment_time=appointment_time,
        status=BookingStatus.CONFIRMED,
        created_at=datetime.now(timezone.utc),
    )
    session.add(booking)
    await session.commit()

    # Re-fetch with relationships
    stmt = (
        select(Booking)
        .where(Booking.id == booking.id)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    result = await session.execute(stmt)
    booking = result.scalar_one()

    logger.info(
        "Internal booking #%d created — student=%d tutor=%d service=%r",
        booking.id, booking.student_id, booking.tutor_id, booking.service_type,
    )
    return booking
