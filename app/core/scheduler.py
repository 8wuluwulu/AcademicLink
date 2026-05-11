"""
AcademicLink — Background Task Scheduler

Uses APScheduler's ``AsyncIOScheduler`` to run periodic jobs:

1. **Morning Briefing** — daily summary of today's bookings for each tutor.
2. **Pre-lesson Reminders** — alerts sent N minutes before a lesson starts.
3. **Automatic Follow-up** — marks lessons as COMPLETED 1 hour after end
   and sends a feedback prompt to the tutor.

The scheduler is started/stopped via the FastAPI lifespan in ``main.py``.
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.database import async_session_factory
from app.db.models import Booking, BookingStatus, Tutor

logger = logging.getLogger(__name__)

# ── Scheduler Instance ───────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="UTC")

# Lesson duration assumption (minutes)
LESSON_DURATION = 60
# Follow-up fires this many minutes after lesson END
FOLLOW_UP_DELAY = 60


# ── Helper: get bot safely ───────────────────────────────────────────

def _get_bot():
    """Import lazily to avoid circular imports at module level."""
    from app.core.bot import get_bot
    return get_bot()


# ═════════════════════════════════════════════════════════════════════
#  Job 1: Morning Briefing
# ═════════════════════════════════════════════════════════════════════


async def morning_briefing_job() -> None:
    """
    Send every active tutor a summary of their CONFIRMED and PENDING
    bookings for today.  Runs daily at ``settings.morning_briefing_hour``.
    """
    bot = _get_bot()
    if bot is None:
        logger.warning("Bot not initialised — skipping morning briefing.")
        return

    from app.bot.formatting import (
        STATUS_EMOJI,
        fmt_booking_compact,
        fmt_date,
    )

    now = datetime.now(timezone.utc)

    async with async_session_factory() as session:
        # Fetch all active tutors
        result = await session.execute(
            select(Tutor).where(Tutor.is_active.is_(True))
        )
        tutors = result.scalars().all()

        for tutor in tutors:
            # Today's boundaries in UTC
            # We use a wide 24-hour window; the tutor sees local times
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)

            result = await session.execute(
                select(Booking)
                .where(
                    Booking.tutor_id == tutor.id,
                    Booking.status.in_([
                        BookingStatus.PENDING,
                        BookingStatus.CONFIRMED,
                    ]),
                    Booking.appointment_time >= day_start,
                    Booking.appointment_time < day_end,
                )
                .options(selectinload(Booking.student))
                .order_by(Booking.appointment_time)
            )
            bookings = result.scalars().all()

            if not bookings:
                text = (
                    f"☀️ <b>Доброе утро!</b>\n\n"
                    f"📅 <b>{fmt_date(now)}</b>\n\n"
                    f"На сегодня занятий нет.\n"
                    f"Хорошего дня! 🌟"
                )
            else:
                confirmed = sum(
                    1 for b in bookings
                    if b.status == BookingStatus.CONFIRMED
                )
                pending = sum(
                    1 for b in bookings
                    if b.status == BookingStatus.PENDING
                )

                lines = [
                    f"☀️ <b>Доброе утро!</b>\n",
                    f"📅 <b>{fmt_date(now)}</b>\n",
                    f"Занятий: <b>{len(bookings)}</b>  "
                    f"(🟢 {confirmed} · 🟡 {pending})\n",
                ]
                for b in bookings:
                    lines.append(fmt_booking_compact(b))

                lines.append(
                    "\n<i>Отправьте /today для подробностей.</i>"
                )
                text = "\n".join(lines)

            try:
                await bot.send_message(
                    chat_id=tutor.tg_id, text=text, parse_mode="HTML",
                )
                logger.info(
                    "Morning briefing sent to tutor tg_id=%d (%d bookings)",
                    tutor.tg_id,
                    len(bookings),
                )
            except Exception as exc:
                logger.error(
                    "Failed to send morning briefing to tg_id=%d: %s",
                    tutor.tg_id,
                    exc,
                )


# ═════════════════════════════════════════════════════════════════════
#  Job 2: Pre-lesson Reminders
# ═════════════════════════════════════════════════════════════════════


async def pre_lesson_reminder_job() -> None:
    """
    Find bookings starting in exactly ``settings.reminder_minutes_before``
    minutes and send a reminder to both tutor and student.

    Runs every 5 minutes.  Uses ``Booking.reminded_at`` to avoid
    sending duplicate reminders.
    """
    bot = _get_bot()
    if bot is None:
        logger.warning("Bot not initialised — skipping reminders.")
        return

    from app.bot.formatting import fmt_full

    now = datetime.now(timezone.utc)
    remind_at = now + timedelta(minutes=settings.reminder_minutes_before)

    # Window: ±5 minutes around the target time (matches the job interval)
    window_start = remind_at - timedelta(minutes=5)
    window_end = remind_at + timedelta(minutes=5)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(
                Booking.status == BookingStatus.CONFIRMED,
                Booking.reminded_at.is_(None),
                Booking.appointment_time >= window_start,
                Booking.appointment_time <= window_end,
            )
            .options(
                selectinload(Booking.student),
                selectinload(Booking.tutor),
            )
        )
        bookings = result.scalars().all()

        for booking in bookings:
            appt = fmt_full(booking.appointment_time)
            student_name = (
                booking.student.full_name if booking.student else "Ученик"
            )

            # ── Notify tutor ─────────────────────────────────────────
            if booking.tutor:
                tutor_text = (
                    f"⏰ <b>Напоминание</b>\n\n"
                    f"Через {settings.reminder_minutes_before} мин. занятие:\n"
                    f"👤 <b>{student_name}</b>\n"
                    f"🕒 {appt}\n"
                    f"📚 {booking.service_type}"
                )
                try:
                    await bot.send_message(
                        chat_id=booking.tutor.tg_id,
                        text=tutor_text,
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.error(
                        "Reminder to tutor tg_id=%d failed: %s",
                        booking.tutor.tg_id,
                        exc,
                    )

            # ── Notify student (if telegram_id is linked) ────────────
            if booking.student and booking.student.telegram_id:
                student_text = (
                    f"⏰ <b>Напоминание</b>\n\n"
                    f"Через {settings.reminder_minutes_before} мин. "
                    f"у вас занятие:\n"
                    f"🕒 {appt}\n"
                    f"📚 {booking.service_type}\n\n"
                    f"<i>До встречи!</i>"
                )
                try:
                    await bot.send_message(
                        chat_id=booking.student.telegram_id,
                        text=student_text,
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.error(
                        "Reminder to student tg_id=%d failed: %s",
                        booking.student.telegram_id,
                        exc,
                    )

            # Mark as reminded
            booking.reminded_at = datetime.now(timezone.utc)
            logger.info("Reminder sent for booking #%d", booking.id)

        await session.commit()


# ═════════════════════════════════════════════════════════════════════
#  Job 3: Automatic Follow-up
# ═════════════════════════════════════════════════════════════════════


async def follow_up_job() -> None:
    """
    Find CONFIRMED bookings whose lesson ended ≥1 hour ago
    (appointment_time + LESSON_DURATION + FOLLOW_UP_DELAY).

    Marks them as COMPLETED and sends a feedback prompt to the tutor.
    Uses ``Booking.followed_up_at`` to avoid duplicates.
    """
    bot = _get_bot()
    if bot is None:
        logger.warning("Bot not initialised — skipping follow-ups.")
        return

    now = datetime.now(timezone.utc)
    # A lesson that started at T ends at T + LESSON_DURATION.
    # Follow-up fires FOLLOW_UP_DELAY minutes after that.
    cutoff = now - timedelta(minutes=LESSON_DURATION + FOLLOW_UP_DELAY)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(
                Booking.status == BookingStatus.CONFIRMED,
                Booking.followed_up_at.is_(None),
                Booking.appointment_time <= cutoff,
            )
            .options(
                selectinload(Booking.student),
                selectinload(Booking.tutor),
            )
        )
        bookings = result.scalars().all()

        for booking in bookings:
            # Transition to COMPLETED
            booking.status = BookingStatus.COMPLETED
            booking.followed_up_at = datetime.now(timezone.utc)

            student_name = (
                booking.student.full_name if booking.student else "Ученик"
            )

            if booking.tutor:
                text = (
                    f"📝 <b>Как прошло занятие?</b>\n\n"
                    f"👤 {student_name}\n"
                    f"📚 {booking.service_type}\n\n"
                    f"Занятие автоматически отмечено как ✅ завершённое.\n"
                    f"<i>Отправьте /today для просмотра расписания.</i>"
                )
                try:
                    await bot.send_message(
                        chat_id=booking.tutor.tg_id,
                        text=text,
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.error(
                        "Follow-up to tutor tg_id=%d failed: %s",
                        booking.tutor.tg_id,
                        exc,
                    )

            logger.info(
                "Booking #%d marked COMPLETED (follow-up sent)", booking.id,
            )

        await session.commit()


# ═════════════════════════════════════════════════════════════════════
#  Scheduler Setup
# ═════════════════════════════════════════════════════════════════════


def configure_scheduler() -> None:
    """Register all periodic jobs on the global scheduler instance."""

    # Job 1: Morning briefing — daily
    scheduler.add_job(
        morning_briefing_job,
        "cron",
        hour=settings.morning_briefing_hour,
        minute=0,
        id="morning_briefing",
        replace_existing=True,
    )

    # Job 2: Pre-lesson reminders — every 5 minutes
    scheduler.add_job(
        pre_lesson_reminder_job,
        "interval",
        minutes=5,
        id="pre_lesson_reminders",
        replace_existing=True,
    )

    # Job 3: Automatic follow-up — every 10 minutes
    scheduler.add_job(
        follow_up_job,
        "interval",
        minutes=10,
        id="follow_up",
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured: morning=%02d:00, reminders=every 5m, "
        "follow-up=every 10m",
        settings.morning_briefing_hour,
    )
