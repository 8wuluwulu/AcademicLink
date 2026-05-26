"""
AcademicLink — Google Calendar Sync Service

Uses standard, lightweight HTTP requests via httpx to interface with
the Google Calendar REST API asynchronously.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Booking, BookingStatus, Service, Student, Tutor

logger = logging.getLogger(__name__)


async def _get_access_token(tutor: Tutor, session: AsyncSession) -> str | None:
    """
    Get the Google access token for the tutor.
    Automatically refreshes the token using the refresh_token if expired or if we encounter a 401.
    """
    if not tutor.google_token_json:
        return None

    try:
        tokens = json.loads(tutor.google_token_json)
    except Exception as exc:
        logger.error("Failed to parse Google OAuth tokens for tutor #%d: %s", tutor.id, exc)
        return None

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")

    if not access_token:
        return None

    # Check if we have an expiration timestamp
    # (Typically expires_in is in seconds, e.g. 3600. If we don't track expires_at, we can rely on 401 retry)
    return access_token


async def _refresh_access_token(tutor: Tutor, session: AsyncSession) -> str | None:
    """
    Refresh the tutor's Google OAuth 2.0 access token using their refresh_token.
    """
    if not tutor.google_token_json:
        return None

    try:
        tokens = json.loads(tutor.google_token_json)
    except Exception as exc:
        logger.error("Failed to parse tokens for refresh: %s", exc)
        return None

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        logger.warning("No refresh token found for tutor #%d — cannot refresh", tutor.id)
        return None

    if not settings.google_client_id or not settings.google_client_secret:
        logger.error("Google client credentials are not configured — cannot refresh")
        return None

    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(token_url, data=data, timeout=10.0)
            if res.status_code != 200:
                logger.error("Failed to refresh Google token for tutor #%d: %s", tutor.id, res.text)
                return None
            
            new_tokens = res.json()
            # Update tokens while preserving the original refresh token (Google doesn't always return it on refresh)
            tokens.update(new_tokens)
            tutor.google_token_json = json.dumps(tokens)
            await session.commit()
            
            logger.info("Successfully refreshed Google token for tutor #%d", tutor.id)
            return tokens["access_token"]
    except Exception as exc:
        logger.error("Error refreshing token for tutor #%d: %s", tutor.id, exc)
        return None


async def _execute_google_request(
    tutor: Tutor,
    session: AsyncSession,
    method: str,
    url: str,
    json_data: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response | None:
    """
    Execute a Google API request with automatic 401 Unauthorized token refreshing.
    """
    access_token = await _get_access_token(tutor, session)
    if not access_token:
        return None

    headers = {"Authorization": f"Bearer {access_token}"}
    
    try:
        async with httpx.AsyncClient() as client:
            # Try original request
            response = await client.request(
                method, url, headers=headers, json=json_data, params=params, timeout=10.0
            )
            
            # If 401 Unauthorized, refresh the token and retry once
            if response.status_code == 401:
                logger.info("Access token expired for tutor #%d — attempting refresh", tutor.id)
                new_token = await _refresh_access_token(tutor, session)
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                    response = await client.request(
                        method, url, headers=headers, json=json_data, params=params, timeout=10.0
                    )
            
            return response
    except Exception as exc:
        logger.error("Google API request exception for tutor #%d: %s", tutor.id, exc)
        return None


# ── Booking Sync Functions ───────────────────────────────────────────

async def sync_booking_to_calendar(session: AsyncSession, booking: Booking) -> None:
    """
    Synchronise a booking to the tutor's Google Calendar.
    Inserts a new event or updates an existing one if google_event_id is present.
    """
    tutor = booking.tutor or await session.get(Tutor, booking.tutor_id)
    if not tutor or not tutor.google_token_json:
        return  # Integration is not set up

    student = booking.student or await session.get(Student, booking.student_id)
    student_name = booking.student_name_snapshot or (student.full_name if student else "Неизвестно")
    student_phone = student.phone if student else "—"
    pay_method = "💵 Наличные" if booking.payment_method == "cash" else "💳 Перевод на карту"

    # Get service duration
    service = await session.get(Service, booking.service_id) if booking.service_id else None
    duration = service.duration if service else 60

    start_time = booking.appointment_time
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    end_time = start_time + timedelta(minutes=duration)

    summary = f"📚 {booking.service_type} — {student_name}"
    description = (
        f"👤 Ученик: {student_name}\n"
        f"📞 Телефон: {student_phone}\n"
        f"💰 Оплата: {pay_method}\n"
        f"🔄 Статус: {booking.status.value}\n"
    )
    if tutor.meeting_link:
        description += f"🔗 Ссылка на урок: {tutor.meeting_link}\n"

    event_payload = {
        "summary": summary,
        "description": description,
        "start": {
            "dateTime": start_time.isoformat(),
            "timeZone": "UTC",
        },
        "end": {
            "dateTime": end_time.isoformat(),
            "timeZone": "UTC",
        },
    }

    # Decide whether to INSERT or UPDATE
    calendar_id = tutor.google_calendar_id or "primary"
    
    if booking.google_event_id:
        # UPDATE
        url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{booking.google_event_id}"
        res = await _execute_google_request(tutor, session, "PUT", url, json_data=event_payload)
        if res and res.status_code == 200:
            logger.info("Successfully updated Google Calendar event %s for booking #%d", booking.google_event_id, booking.id)
        elif res and res.status_code == 404:
            # Event was deleted on Google Calendar, reset event ID and recreate
            booking.google_event_id = None
            await sync_booking_to_calendar(session, booking)
        else:
            logger.error("Failed to update Google event: %s", res.text if res else "No response")
    else:
        # INSERT
        url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
        res = await _execute_google_request(tutor, session, "POST", url, json_data=event_payload)
        if res and res.status_code == 200:
            event_data = res.json()
            booking.google_event_id = event_data.get("id")
            await session.commit()
            logger.info("Successfully created Google Calendar event %s for booking #%d", booking.google_event_id, booking.id)
        else:
            logger.error("Failed to create Google event: %s", res.text if res else "No response")


async def delete_calendar_event(session: AsyncSession, booking: Booking) -> None:
    """
    Delete the Google Calendar event mapped to a booking (e.g. if the booking is cancelled).
    """
    if not booking.google_event_id:
        return

    tutor = booking.tutor or await session.get(Tutor, booking.tutor_id)
    if not tutor or not tutor.google_token_json:
        return

    calendar_id = tutor.google_calendar_id or "primary"
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{booking.google_event_id}"
    
    res = await _execute_google_request(tutor, session, "DELETE", url)
    if res and res.status_code in (200, 204):
        logger.info("Successfully deleted Google Calendar event %s for booking #%d", booking.google_event_id, booking.id)
        booking.google_event_id = None
        await session.commit()
    elif res and res.status_code == 404:
        # Already deleted
        booking.google_event_id = None
        await session.commit()
    else:
        logger.error("Failed to delete Google event: %s", res.text if res else "No response")


# ── Available Slots Sync (Fetch busy slots) ───────────────────────────

async def get_busy_slots_from_calendar(
    session: AsyncSession,
    tutor_id: int,
    start_date: datetime,
    end_date: datetime,
) -> list[tuple[datetime, datetime]]:
    """
    Fetch busy time intervals (events) from the tutor's Google Calendar for the given date range.
    Returns a list of tuples: (start_datetime, end_datetime) in UTC timezone.
    """
    tutor = await session.get(Tutor, tutor_id)
    if not tutor or not tutor.google_token_json:
        return []

    calendar_id = tutor.google_calendar_id or "primary"
    url = f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
    
    # Format times in RFC3339 format
    params = {
        "timeMin": start_date.isoformat(),
        "timeMax": end_date.isoformat(),
        "singleEvents": "true",
        "orderBy": "startTime",
    }

    res = await _execute_google_request(tutor, session, "GET", url, params=params)
    if not res or res.status_code != 200:
        logger.error("Failed to fetch busy slots from Google Calendar: %s", res.text if res else "No response")
        return []

    events_data = res.json().get("items", [])
    busy_intervals = []

    for event in events_data:
        start_data = event.get("start", {})
        end_data = event.get("end", {})
        
        # Support both 'dateTime' (specific time) and 'date' (all-day events)
        start_str = start_data.get("dateTime") or start_data.get("date")
        end_str = end_data.get("dateTime") or end_data.get("date")

        if not start_str or not end_str:
            continue

        try:
            # Parse datetime string
            # date format: 2026-05-25 (all day) -> parse as midnight UTC
            if "T" not in start_str:
                start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                end_dt = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            else:
                # dateTime format: 2026-05-25T14:00:00+03:00 or 2026-05-25T14:00:00Z
                # Replace Z with +00:00 for fromisoformat
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(timezone.utc)
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(timezone.utc)
            
            busy_intervals.append((start_dt, end_dt))
        except Exception as exc:
            logger.error("Failed to parse Google event timestamps (%s -> %s): %s", start_str, end_str, exc)

    return busy_intervals
