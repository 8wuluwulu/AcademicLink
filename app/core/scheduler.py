"""
AcademicLink — Background Task Scheduler

Uses APScheduler's ``AsyncIOScheduler`` to run periodic jobs:

1. **Pre-lesson Reminders** — alerts sent N minutes before a lesson starts.
   (Default is 100 minutes / 1h 40m).

The scheduler is started/stopped via the FastAPI lifespan in ``main.py``.
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.database import async_session_factory
from app.db.models import Booking, BookingStatus

logger = logging.getLogger(__name__)

# ── Scheduler Instance ───────────────────────────────────────────────
scheduler = AsyncIOScheduler(timezone="UTC")


# ── Helper: get bot safely ───────────────────────────────────────────

def _get_bot():
    """Import lazily to avoid circular imports at module level."""
    from app.core.bot import get_bot
    return get_bot()


# ═════════════════════════════════════════════════════════════════════
#  Job: Pre-lesson Reminders
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
#  Scheduler Setup
# ═════════════════════════════════════════════════════════════════════


def configure_scheduler() -> None:
    """Register periodic jobs on the global scheduler instance."""

    # Job: Pre-lesson reminders — every 5 minutes
    scheduler.add_job(
        pre_lesson_reminder_job,
        "interval",
        minutes=5,
        id="pre_lesson_reminders",
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured: reminders=every 5m (lead time %d min)",
        settings.reminder_minutes_before,
    )
