"""
AcademicLink — Booking Service

Core business logic for creating and managing bookings using Services.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AvailabilitySlot, Booking, BookingStatus, Service, Student, Tutor, TutorAbsence

logger = logging.getLogger(__name__)

# ── Validation Helpers ───────────────────────────────────────────────


async def check_tutor_absence(
    session: AsyncSession,
    *,
    tutor_id: int,
    appointment_time: datetime,
) -> None:
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
            f"Репетитор отсутствует в это время{reason_text}."
        )


async def check_availability(
    session: AsyncSession,
    *,
    tutor_id: int,
    appointment_time: datetime,
) -> None:
    from app.bot.formatting import MSK
    local_time = appointment_time.astimezone(MSK)
    weekday = local_time.weekday()
    local_clock = local_time.time()

    stmt = select(AvailabilitySlot).where(
        AvailabilitySlot.tutor_id == tutor_id,
        AvailabilitySlot.weekday == weekday,
    )
    result = await session.execute(stmt)
    slots = result.scalars().all()

    if not slots:
        raise ValueError("Репетитор не принимает в этот день.")

    for slot in slots:
        if slot.start_time <= local_clock < slot.end_time:
            return

    raise ValueError("Выбранное время вне рабочих часов репетитора.")


async def check_double_booking(
    session: AsyncSession,
    *,
    tutor_id: int,
    appointment_time: datetime,
    lesson_duration: int,
    buffer_time: int,
    exclude_booking_id: int | None = None,
) -> None:
    # 1. Fetch bookings within ±1 day around the appointment time to be fully safe
    day_start = appointment_time.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    day_end = appointment_time.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=2)

    stmt = select(Booking, Service).join(Service, isouter=True).where(
        Booking.tutor_id == tutor_id,
        Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.PENDING]),
        Booking.appointment_time >= day_start,
        Booking.appointment_time < day_end,
    )
    if exclude_booking_id is not None:
        stmt = stmt.where(Booking.id != exclude_booking_id)
        
    result = await session.execute(stmt)
    existing_data = result.all()

    new_start = appointment_time
    if new_start.tzinfo is None:
        new_start = new_start.replace(tzinfo=timezone.utc)
    new_end = new_start + timedelta(minutes=lesson_duration + buffer_time)

    for b, s in existing_data:
        b_start = b.appointment_time
        if b_start.tzinfo is None:
            b_start = b_start.replace(tzinfo=timezone.utc)
        b_dur = s.duration if s else 60
        b_buf = s.buffer_time if s else 0
        b_end = b_start + timedelta(minutes=b_dur + b_buf)

        # Precise overlap check: start1 < end2 and end1 > start2
        if new_start < b_end and new_end > b_start:
            from app.bot.formatting import MSK
            conflict_local = b_start.astimezone(MSK)
            raise ValueError(
                f"Временной конфликт: у репетитора уже есть занятие в {conflict_local:%H:%M}."
            )


# ── Main Service Functions ───────────────────────────────────────────


async def create_booking_from_web(
    session: AsyncSession,
    *,
    full_name: str,
    phone: str | None = None,
    service_id: int,
    appointment_time: datetime,
    tutor_id: int,
    telegram_username: str | None = None,
    payment_method: str = "cash",
) -> Booking:
    if appointment_time.tzinfo is None:
        appointment_time = appointment_time.replace(tzinfo=timezone.utc)

    # 1. Resolve Service
    service = await session.get(Service, service_id)
    if not service or service.tutor_id != tutor_id or not service.is_active:
        raise ValueError("Выбранная услуга не доступна.")

    # 2. Resolve Student
    student = None
    if telegram_username:
        clean_username = telegram_username.lstrip("@")
        stmt = select(Student).where(Student.telegram_username == clean_username)
        result = await session.execute(stmt)
        student = result.scalar_one_or_none()

    if student is None and phone:
        stmt = select(Student).where(Student.phone == phone)
        result = await session.execute(stmt)
        student = result.scalar_one_or_none()

    if student is None:
        # Generate a unique pseudo-phone number if not provided
        if not phone:
            uname_part = telegram_username.lstrip("@") if telegram_username else "unknown"
            phone = f"+999{abs(hash(uname_part)) % 1000000000:09d}"

        student = Student(
            full_name=full_name,
            phone=phone,
            telegram_username=telegram_username.lstrip("@") if telegram_username else None,
        )
        session.add(student)
        await session.flush()
    else:
        if not student.is_active:
            student.is_active = True
        student.full_name = full_name
        if telegram_username:
            student.telegram_username = telegram_username.lstrip("@")

    # 3. Resolve Tutor
    tutor = await session.get(Tutor, tutor_id)
    if not tutor or not tutor.is_active:
        raise ValueError("Репетитор не принимает записи.")

    # 4. Validations
    await check_availability(session, tutor_id=tutor_id, appointment_time=appointment_time)
    await check_tutor_absence(session, tutor_id=tutor_id, appointment_time=appointment_time)
    await check_double_booking(
        session,
        tutor_id=tutor_id,
        appointment_time=appointment_time,
        lesson_duration=service.duration,
        buffer_time=service.buffer_time,
    )

    # 5. Create Booking
    booking = Booking(
        student_id=student.id,
        tutor_id=tutor.id,
        service_id=service.id,
        service_type=service.name,
        student_name_snapshot=full_name,
        appointment_time=appointment_time,
        status=BookingStatus.PENDING,
        created_at=datetime.now(timezone.utc),
        payment_method=payment_method,
    )
    session.add(booking)
    await session.commit()

    stmt = (
        select(Booking)
        .where(Booking.id == booking.id)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    result = await session.execute(stmt)
    return result.scalar_one()


async def get_available_slots(
    session: AsyncSession,
    *,
    tutor_id: int,
    service_id: int,
    date: datetime,
) -> list[str]:
    from app.bot.formatting import MSK
    
    tutor = await session.get(Tutor, tutor_id)
    if not tutor or not tutor.is_active:
        return []

    service = await session.get(Service, service_id)
    if not service or service.tutor_id != tutor_id:
        return []

    duration = service.duration
    buffer = service.buffer_time
    total_needed = duration + buffer

    local_date = date.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    weekday = local_date.weekday()

    stmt = select(AvailabilitySlot).where(
        AvailabilitySlot.tutor_id == tutor_id,
        AvailabilitySlot.weekday == weekday,
    )
    res = await session.execute(stmt)
    slots = res.scalars().all()
    if not slots:
        return []

    day_start = local_date.astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    
    # Get bookings with their associated service durations
    stmt = select(Booking, Service).join(Service, isouter=True).where(
        Booking.tutor_id == tutor_id,
        Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.PENDING]),
        Booking.appointment_time >= day_start,
        Booking.appointment_time < day_end,
    )
    res = await session.execute(stmt)
    existing_data = res.all()

    stmt = select(TutorAbsence).where(
        TutorAbsence.tutor_id == tutor_id,
        TutorAbsence.end_time >= day_start,
        TutorAbsence.start_time < day_end,
    )
    res = await session.execute(stmt)
    absences = res.scalars().all()

    # Get busy slots from Google Calendar
    from app.services.google_calendar_service import get_busy_slots_from_calendar
    try:
        busy_calendar_intervals = await get_busy_slots_from_calendar(
            session, tutor_id=tutor_id, start_date=day_start, end_date=day_end
        )
    except Exception as exc:
        logger.error("Failed to fetch busy slots from Google Calendar: %s", exc)
        busy_calendar_intervals = []

    available_times = []
    for slot in slots:
        current_time = local_date.replace(hour=slot.start_time.hour, minute=slot.start_time.minute)
        end_time = local_date.replace(hour=slot.end_time.hour, minute=slot.end_time.minute)
        
        now_local = datetime.now(MSK)
        if current_time < now_local:
            minutes = (now_local.minute // 15 + 1) * 15
            current_time = now_local.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minutes)
            if current_time < local_date.replace(hour=slot.start_time.hour, minute=slot.start_time.minute):
                current_time = local_date.replace(hour=slot.start_time.hour, minute=slot.start_time.minute)

        while current_time + timedelta(minutes=duration) <= end_time:
            candidate_start = current_time.astimezone(timezone.utc)
            candidate_end = candidate_start + timedelta(minutes=total_needed)
            
            overlap = False
            for b, s in existing_data:
                # Skip sync events that correspond to the current booking if we exclude it
                # (Not needed in get_available_slots, but good pattern)
                b_start = b.appointment_time
                b_dur = s.duration if s else 60
                b_buf = s.buffer_time if s else 0
                b_end = b_start + timedelta(minutes=b_dur + b_buf)
                
                if candidate_start < b_end and candidate_end > b_start:
                    overlap = True
                    break
            
            if not overlap:
                for a in absences:
                    if candidate_start < a.end_time and candidate_end > a.start_time:
                        overlap = True
                        break

            if not overlap:
                for c_start, c_end in busy_calendar_intervals:
                    # Skip events that might be sync placeholders or mismatch
                    # If this slot overlaps with any busy event on tutor's calendar, block it
                    if candidate_start < c_end and candidate_end > c_start:
                        overlap = True
                        break
            
            if not overlap:
                available_times.append(current_time.strftime("%H:%M"))
            
            current_time += timedelta(minutes=15)

    return sorted(list(set(available_times)))


async def reschedule_booking(
    session: AsyncSession,
    *,
    booking_id: int,
    new_appointment_time: datetime,
) -> tuple[Booking, datetime]:
    if new_appointment_time.tzinfo is None:
        new_appointment_time = new_appointment_time.replace(tzinfo=timezone.utc)

    stmt = (
        select(Booking)
        .where(Booking.id == booking_id)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    result = await session.execute(stmt)
    booking = result.scalar_one_or_none()

    if not booking:
        raise ValueError("Запись не найдена.")
    
    # Fetch service manually if not loaded
    service = await session.get(Service, booking.service_id) if booking.service_id else None
    duration = service.duration if service else 60
    buffer = service.buffer_time if service else 0

    await check_availability(session, tutor_id=booking.tutor_id, appointment_time=new_appointment_time)
    await check_tutor_absence(session, tutor_id=booking.tutor_id, appointment_time=new_appointment_time)
    await check_double_booking(
        session,
        tutor_id=booking.tutor_id,
        appointment_time=new_appointment_time,
        lesson_duration=duration,
        buffer_time=buffer,
        exclude_booking_id=booking.id,
    )

    old_time = booking.appointment_time
    booking.appointment_time = new_appointment_time
    booking.status = BookingStatus.CONFIRMED
    await session.commit()

    # Sync with Google Calendar if enabled
    from app.services.google_calendar_service import sync_booking_to_calendar
    try:
        await sync_booking_to_calendar(session, booking)
    except Exception as exc:
        logger.error("Failed to sync rescheduled booking #%d to Google Calendar: %s", booking.id, exc)

    await session.refresh(booking, attribute_names=["student", "tutor"])
    return booking, old_time


async def create_booking_internal(
    session: AsyncSession,
    *,
    student_id: int,
    tutor_id: int,
    service_id: int,
    appointment_time: datetime,
) -> Booking:
    if appointment_time.tzinfo is None:
        appointment_time = appointment_time.replace(tzinfo=timezone.utc)

    service = await session.get(Service, service_id)
    if not service or service.tutor_id != tutor_id:
        raise ValueError("Услуга не найдена.")

    await check_availability(session, tutor_id=tutor_id, appointment_time=appointment_time)
    await check_tutor_absence(session, tutor_id=tutor_id, appointment_time=appointment_time)
    await check_double_booking(
        session,
        tutor_id=tutor_id,
        appointment_time=appointment_time,
        lesson_duration=service.duration,
        buffer_time=service.buffer_time,
    )

    booking = Booking(
        student_id=student_id,
        tutor_id=tutor_id,
        service_id=service_id,
        service_type=service.name,
        appointment_time=appointment_time,
        status=BookingStatus.CONFIRMED,
        created_at=datetime.now(timezone.utc),
    )
    session.add(booking)
    await session.commit()

    # Sync with Google Calendar if enabled
    from app.services.google_calendar_service import sync_booking_to_calendar
    try:
        await sync_booking_to_calendar(session, booking)
    except Exception as exc:
        logger.error("Failed to sync internal booking #%d to Google Calendar: %s", booking.id, exc)

    stmt = (
        select(Booking)
        .where(Booking.id == booking.id)
        .options(selectinload(Booking.student), selectinload(Booking.tutor))
    )
    result = await session.execute(stmt)
    return result.scalar_one()
