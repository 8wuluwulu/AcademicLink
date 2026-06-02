"""
AcademicLink — Background Task Scheduler

Uses APScheduler's ``AsyncIOScheduler`` to run periodic jobs:

1. **Pre-lesson Reminders** — alerts sent N minutes before a lesson starts.
   (Default is 100 minutes / 1h 40m).
2. **24-hour Reminders** — alerts sent ~24 hours before a lesson starts.

The scheduler is started/stopped via the FastAPI lifespan in ``main.py``.

Both reminder types respect the ``wants_reminders`` opt-in flag on
Tutor and Student models.
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


# ── Helper: get bot safely ───────────────────────────────────────────

def _get_bot():
    """Import lazily to avoid circular imports at module level."""
    from app.core.bot import get_bot
    return get_bot()


# ═════════════════════════════════════════════════════════════════════
#  Job: Pre-lesson Reminders (~100 min before)
# ═════════════════════════════════════════════════════════════════════


async def pre_lesson_reminder_job() -> None:
    """
    Find bookings starting in exactly ``settings.reminder_minutes_before``
    minutes and send a reminder to both tutor and student.

    Runs every 5 minutes.  Uses ``Booking.reminded_at`` to avoid
    sending duplicate reminders.

    Respects ``wants_reminders`` opt-in flag on both Tutor and Student.
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
            if booking.tutor and booking.tutor.wants_reminders:
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
            if (
                booking.student
                and booking.student.telegram_id
                and booking.student.wants_reminders
            ):
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
#  Job: 24-hour Reminders
# ═════════════════════════════════════════════════════════════════════


async def daily_reminder_job() -> None:
    """
    Find bookings starting in ~24 hours and send a reminder to
    both tutor and student.

    Runs every 30 minutes.  Uses ``Booking.reminded_24h_at`` to avoid
    sending duplicate reminders.

    Respects ``wants_reminders`` opt-in flag on both Tutor and Student.
    """
    bot = _get_bot()
    if bot is None:
        logger.warning("Bot not initialised — skipping 24h reminders.")
        return

    from app.bot.formatting import fmt_full

    now = datetime.now(timezone.utc)
    target = now + timedelta(hours=24)

    # Window: ±15 minutes (matches 30-min job interval)
    window_start = target - timedelta(minutes=15)
    window_end = target + timedelta(minutes=15)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Booking)
            .where(
                Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.PENDING]),
                Booking.reminded_24h_at.is_(None),
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
            if booking.tutor and booking.tutor.wants_reminders:
                tutor_text = (
                    f"📅 <b>Напоминание на завтра</b>\n\n"
                    f"Завтра у вас занятие:\n"
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
                        "24h reminder to tutor tg_id=%d failed: %s",
                        booking.tutor.tg_id,
                        exc,
                    )

            # ── Notify student ────────────────────────────────────────
            if (
                booking.student
                and booking.student.telegram_id
                and booking.student.wants_reminders
            ):
                student_text = (
                    f"📅 <b>Напоминание на завтра</b>\n\n"
                    f"Завтра у вас занятие:\n"
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
                        "24h reminder to student tg_id=%d failed: %s",
                        booking.student.telegram_id,
                        exc,
                    )

            # Mark as reminded
            booking.reminded_24h_at = datetime.now(timezone.utc)
            logger.info("24h reminder sent for booking #%d", booking.id)

        await session.commit()


# ═════════════════════════════════════════════════════════════════════
#  Job: Subscription Renewal Alerts
# ═════════════════════════════════════════════════════════════════════


async def subscription_renewal_alert_job() -> None:
    """
    Find all active tutors whose subscription expires in exactly 3 days or 1 day
    and send them a Telegram alert:
    "⏰ Your subscription expires in {X} days. Please renew to avoid scheduling interruptions."
    """
    bot = _get_bot()
    if bot is None:
        logger.warning("Bot not initialised — skipping subscription renewal alerts.")
        return

    now = datetime.now(timezone.utc)

    async with async_session_factory() as session:
        result = await session.execute(
            select(Tutor).where(
                Tutor.is_active == True,
                Tutor.subscription_expires_at.is_not(None),
            )
        )
        tutors = result.scalars().all()

        for tutor in tutors:
            delta = tutor.subscription_expires_at - now
            hours_left = delta.total_seconds() / 3600.0

            days_left = None
            if 48.0 < hours_left <= 72.0:
                days_left = 3
            elif 0.0 < hours_left <= 24.0:
                days_left = 1

            if days_left is not None:
                days_word = "день" if days_left == 1 else "дня"
                text = f"⏰ Ваша подписка истекает через {days_left} {days_word}. Пожалуйста, продлите её, чтобы избежать перерывов в расписании."
                try:
                    await bot.send_message(
                        chat_id=tutor.tg_id,
                        text=text,
                        parse_mode="HTML",
                    )
                    logger.info("Sent subscription warning to tutor tg_id=%d (%d days left)", tutor.tg_id, days_left)
                except Exception as exc:
                    logger.error("Failed to send subscription renewal alert to tutor tg_id=%d: %s", tutor.tg_id, exc)


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

    # Job: 24-hour reminders — every 30 minutes
    scheduler.add_job(
        daily_reminder_job,
        "interval",
        minutes=30,
        id="daily_reminders",
        replace_existing=True,
    )

    # Job: Subscription renewal alerts — every 24 hours (daily at 09:00 UTC)
    scheduler.add_job(
        subscription_renewal_alert_job,
        "cron",
        hour=9,
        minute=0,
        id="subscription_renewal_alerts",
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured: pre-lesson=every 5m, 24h=every 30m, sub_alert=daily 09:00 UTC",
    )
