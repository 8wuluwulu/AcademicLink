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
            sub_expires = tutor.subscription_expires_at
            if sub_expires and sub_expires.tzinfo is None:
                sub_expires = sub_expires.replace(tzinfo=timezone.utc)
            delta = sub_expires - now
            hours_left = delta.total_seconds() / 3600.0

            days_left = None
            if 48.0 < hours_left <= 72.0:
                days_left = 3
            elif 0.0 < hours_left <= 24.0:
                days_left = 1

            if days_left is not None:
                # BUG #017 fix: skip if already warned in the last 20 hours
                # to prevent duplicate alerts on application restart
                if tutor.subscription_warned_at:
                    warned_at = tutor.subscription_warned_at
                    if warned_at.tzinfo is None:
                        warned_at = warned_at.replace(tzinfo=timezone.utc)
                    if (now - warned_at).total_seconds() < 20 * 3600:
                        continue

                days_word = "день" if days_left == 1 else "дня"
                text = f"⏰ Подписка истекает через {days_left} {days_word}. Не забудьте продлить!"
                try:
                    await bot.send_message(
                        chat_id=tutor.tg_id,
                        text=text,
                        parse_mode="HTML",
                    )
                    tutor.subscription_warned_at = now
                    logger.info("Sent subscription warning to tutor tg_id=%d (%d days left)", tutor.tg_id, days_left)
                except Exception as exc:
                    logger.error("Failed to send subscription renewal alert to tutor tg_id=%d: %s", tutor.tg_id, exc)

        await session.commit()


# ═════════════════════════════════════════════════════════════════════
#  Job: Sync Google Calendar changes (Reschedule / Cancellation)
# ═════════════════════════════════════════════════════════════════════

async def sync_google_calendar_changes_job() -> None:
    """
    Find tutors with Google Calendar integrated, retrieve their recent/upcoming events,
    and update bookings in our database if rescheduled or cancelled directly in Google Calendar.
    """
    bot = _get_bot()

    async with async_session_factory() as session:
        tutors_stmt = select(Tutor).where(Tutor.google_token_json.is_not(None))
        tutors_res = await session.execute(tutors_stmt)
        tutors = tutors_res.scalars().all()

        for tutor in tutors:
            try:
                calendar_id = tutor.google_calendar_id or "primary"
                url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

                # Check bookings from 7 days ago to 60 days in the future
                now = datetime.now(timezone.utc)
                time_min = (now - timedelta(days=7)).replace(microsecond=0).isoformat()
                time_max = (now + timedelta(days=60)).replace(microsecond=0).isoformat()

                params = {
                    "timeMin": time_min,
                    "timeMax": time_max,
                    "singleEvents": "true",
                    "showDeleted": "true",
                }

                from app.services.google_calendar_service import _execute_google_request
                res = await _execute_google_request(tutor, session, "GET", url, params=params)
                if not res or res.status_code != 200:
                    logger.error("Failed to fetch Google Calendar events for tutor %d: %s", tutor.id, res.text if res else "No response")
                    continue

                events = res.json().get("items", [])

                for event in events:
                    event_id = event.get("id")
                    if not event_id:
                        continue

                    # Find booking by google_event_id
                    booking_stmt = (
                        select(Booking)
                        .where(Booking.google_event_id == event_id)
                        .options(selectinload(Booking.student), selectinload(Booking.tutor))
                    )
                    booking_res = await session.execute(booking_stmt)
                    booking = booking_res.scalar_one_or_none()

                    if not booking:
                        continue

                    # 1. Handle Cancellation in Google Calendar
                    if event.get("status") == "cancelled":
                        if booking.status != BookingStatus.CANCELLED:
                            old_time = booking.appointment_time
                            booking.status = BookingStatus.CANCELLED
                            booking.google_event_id = None
                            await session.commit()

                            logger.info("Booking #%d cancelled via Google Calendar sync", booking.id)

                            if bot:
                                from app.bot.formatting import fmt_full
                                time_str = fmt_full(old_time)

                                # Notify Student
                                if booking.student and booking.student.telegram_id:
                                    student_msg = (
                                        f"🔴 <b>Занятие отменено преподавателем!</b>\n\n"
                                        f"👨‍🏫 <b>Преподаватель:</b> {tutor.name}\n"
                                        f"🏷 <b>Услуга:</b> {booking.service_type}\n"
                                        f"🕒 <b>Было запланировано на:</b> {time_str}\n\n"
                                        f"Занятие отменено."
                                    )
                                    try:
                                        await bot.send_message(chat_id=booking.student.telegram_id, text=student_msg, parse_mode="HTML")
                                    except Exception as e:
                                        logger.error("Failed to notify student of GCal cancellation: %s", e)

                                # Notify Tutor
                                if tutor.tg_id:
                                    tutor_msg = (
                                        f"🔴 <b>Занятие отменено из Google Календаря!</b>\n\n"
                                        f"👤 <b>Ученик:</b> {booking.student_name_snapshot or booking.student.full_name}\n"
                                        f"🕒 <b>Было запланировано на:</b> {time_str}\n\n"
                                        f"Запись отменена в системе AcademicLink."
                                    )
                                    try:
                                        await bot.send_message(chat_id=tutor.tg_id, text=tutor_msg, parse_mode="HTML")
                                    except Exception as e:
                                        logger.error("Failed to notify tutor of GCal cancellation: %s", e)
                        continue

                    # 2. Handle Rescheduling in Google Calendar
                    start_data = event.get("start", {})
                    start_str = start_data.get("dateTime") or start_data.get("date")
                    if not start_str:
                        continue

                    try:
                        if "T" not in start_str:
                            event_start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        else:
                            event_start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(timezone.utc)
                    except Exception as exc:
                        logger.error("Failed to parse start time for event %s: %s", event_id, exc)
                        continue

                    # Compare booking time
                    booking_time = booking.appointment_time
                    if booking_time.tzinfo is None:
                        booking_time = booking_time.replace(tzinfo=timezone.utc)
                    else:
                        booking_time = booking_time.astimezone(timezone.utc)

                    if booking_time != event_start_dt:
                        if booking.status == BookingStatus.CONFIRMED:
                            old_time = booking.appointment_time
                            booking.appointment_time = event_start_dt
                            await session.commit()

                            logger.info("Booking #%d rescheduled via Google Calendar sync (from %s to %s)", booking.id, old_time, event_start_dt)

                            if bot:
                                from app.bot.formatting import fmt_full
                                old_time_str = fmt_full(old_time)
                                new_time_str = fmt_full(event_start_dt)

                                # Notify Student
                                if booking.student and booking.student.telegram_id:
                                    student_msg = (
                                        f"🕒 <b>Преподаватель перенёс занятие!</b>\n\n"
                                        f"👨‍🏫 <b>Преподаватель:</b> {tutor.name}\n"
                                        f"🏷 <b>Услуга:</b> {booking.service_type}\n"
                                        f"❌ <b>Было:</b> {old_time_str}\n"
                                        f"✅ <b>Стало:</b> {new_time_str}\n\n"
                                        f"Ждем вас на занятии!"
                                    )
                                    try:
                                        await bot.send_message(chat_id=booking.student.telegram_id, text=student_msg, parse_mode="HTML")
                                    except Exception as e:
                                        logger.error("Failed to notify student of GCal reschedule: %s", e)

                                # Notify Tutor
                                if tutor.tg_id:
                                    tutor_msg = (
                                        f"✅ <b>Занятие успешно перенесено из Google Календаря!</b>\n\n"
                                        f"👤 <b>Ученик:</b> {booking.student_name_snapshot or booking.student.full_name}\n"
                                        f"❌ <b>Было:</b> {old_time_str}\n"
                                        f"✅ <b>Стало:</b> {new_time_str}\n\n"
                                        f"Данные в системе AcademicLink обновлены."
                                    )
                                    try:
                                        await bot.send_message(chat_id=tutor.tg_id, text=tutor_msg, parse_mode="HTML")
                                    except Exception as e:
                                        logger.error("Failed to notify tutor of GCal reschedule: %s", e)
            except Exception as e:
                logger.error("Error syncing calendar changes for tutor %d: %s", tutor.id, e)


async def pending_bookings_reminder_job() -> None:
    """
    Find all bookings in PENDING status, group them by tutor,
    and send a Telegram reminder to the tutor to confirm.
    Runs every 2 hours.
    """
    bot = _get_bot()
    if bot is None:
        logger.warning("Bot not initialised — skipping pending bookings reminders.")
        return

    from sqlalchemy import select, func
    from app.db.models import Booking, BookingStatus, Tutor

    async with async_session_factory() as session:
        stmt = (
            select(Booking.tutor_id, func.count(Booking.id))
            .where(Booking.status == BookingStatus.PENDING)
            .group_by(Booking.tutor_id)
        )
        result = await session.execute(stmt)
        pending_data = result.all()

        for tutor_id, count in pending_data:
            tutor = await session.get(Tutor, tutor_id)
            if tutor and tutor.tg_id:
                text = (
                    f"🔔 <b>Напоминание о новых заявках</b>\n\n"
                    f"У вас есть неподтвержденные заявки на обучение (всего: <b>{count}</b>).\n"
                    f"Пожалуйста, подтвердите или отклоните их в разделе «🟡 Новые заявки» в меню."
                )
                try:
                    await bot.send_message(
                        chat_id=tutor.tg_id,
                        text=text,
                        parse_mode="HTML",
                    )
                    logger.info("Sent pending bookings warning to tutor tg_id=%d (%d pending)", tutor.tg_id, count)
                except Exception as exc:
                    logger.error("Failed to send pending bookings warning to tutor tg_id=%d: %s", tutor.tg_id, exc)


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

    # Job: Sync Google Calendar changes — every 1 minute
    scheduler.add_job(
        sync_google_calendar_changes_job,
        "interval",
        minutes=1,
        id="sync_google_calendar_changes",
        replace_existing=True,
    )

    # Job: Pending bookings reminders — every 2 hours
    scheduler.add_job(
        pending_bookings_reminder_job,
        "interval",
        hours=2,
        id="pending_bookings_reminders",
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured: pre-lesson=every 5m, 24h=every 30m, sub_alert=daily 09:00 UTC, gcal_sync=every 1m, pending_remind=every 2h",
    )
