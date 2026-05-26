"""
AcademicLink — Unit & Mock Tests for Google Calendar Service

Covers:
- Early exits when integration is not configured
- Event creation (POST) & updating (PUT) mocking
- Retrieval and parsing of busy slots from calendar (GET)
"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Booking, BookingStatus, Tutor
from app.services.google_calendar_service import (
    sync_booking_to_calendar,
    delete_calendar_event,
    get_busy_slots_from_calendar,
)


@pytest.mark.asyncio
async def test_sync_booking_no_google_token(seeded_session: AsyncSession):
    """If tutor doesn't have Google OAuth enabled, it should exit silently without requests."""
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = None
    await seeded_session.commit()

    booking = Booking(
        id=999,
        tutor_id=1,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime.now(timezone.utc),
        status=BookingStatus.CONFIRMED,
    )

    with patch("httpx.AsyncClient.request") as mock_request:
        await sync_booking_to_calendar(seeded_session, booking)
        mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_sync_booking_new_event(seeded_session: AsyncSession):
    """If no google_event_id is set, a POST request should create the event and save its ID."""
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({
        "access_token": "mock_access_token",
        "refresh_token": "mock_refresh_token"
    })
    await seeded_session.commit()

    booking = Booking(
        id=999,
        tutor_id=1,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        status=BookingStatus.CONFIRMED,
    )
    seeded_session.add(booking)
    await seeded_session.flush()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "google_event_12345"}

    with patch("httpx.AsyncClient.request", return_value=mock_response) as mock_request:
        await sync_booking_to_calendar(seeded_session, booking)
        
        # Verify a POST request to create an event was made
        mock_request.assert_called_once()
        method, url = mock_request.call_args[0]
        assert method == "POST"
        assert "events" in url
        
        # Verify the event ID was written to the booking in DB
        assert booking.google_event_id == "google_event_12345"


@pytest.mark.asyncio
async def test_sync_booking_update_existing_event(seeded_session: AsyncSession):
    """If google_event_id is present, a PUT request should update the event."""
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({
        "access_token": "mock_access_token",
        "refresh_token": "mock_refresh_token"
    })
    await seeded_session.commit()

    booking = Booking(
        id=999,
        tutor_id=1,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        status=BookingStatus.CONFIRMED,
        google_event_id="google_event_12345",
    )
    seeded_session.add(booking)
    await seeded_session.flush()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200

    with patch("httpx.AsyncClient.request", return_value=mock_response) as mock_request:
        await sync_booking_to_calendar(seeded_session, booking)
        
        # Verify a PUT request to update the event was made
        mock_request.assert_called_once()
        method, url = mock_request.call_args[0]
        assert method == "PUT"
        assert "events/google_event_12345" in url


@pytest.mark.asyncio
async def test_delete_calendar_event(seeded_session: AsyncSession):
    """If google_event_id is present, cancelling should call DELETE on the API."""
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({
        "access_token": "mock_access_token",
        "refresh_token": "mock_refresh_token"
    })
    await seeded_session.commit()

    booking = Booking(
        id=999,
        tutor_id=1,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        status=BookingStatus.CANCELLED,
        google_event_id="google_event_12345",
    )
    seeded_session.add(booking)
    await seeded_session.flush()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200

    with patch("httpx.AsyncClient.request", return_value=mock_response) as mock_request:
        await delete_calendar_event(seeded_session, booking)
        
        # Verify a DELETE request was made
        mock_request.assert_called_once()
        method, url = mock_request.call_args[0]
        assert method == "DELETE"
        assert "events/google_event_12345" in url
        
        # Verify the event ID was cleared
        assert booking.google_event_id is None


@pytest.mark.asyncio
async def test_get_busy_slots(seeded_session: AsyncSession):
    """Fetching busy slots should call GET and correctly parse timestamps."""
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({
        "access_token": "mock_access_token",
        "refresh_token": "mock_refresh_token"
    })
    await seeded_session.commit()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "items": [
            {
                "start": {"dateTime": "2026-05-26T12:00:00Z"},
                "end": {"dateTime": "2026-05-26T13:00:00Z"}
            },
            {
                "start": {"date": "2026-05-27"},
                "end": {"date": "2026-05-28"}
            }
        ]
    }

    start_date = datetime(2026, 5, 25, tzinfo=timezone.utc)
    end_date = datetime(2026, 5, 29, tzinfo=timezone.utc)

    with patch("httpx.AsyncClient.request", return_value=mock_response) as mock_request:
        busy_slots = await get_busy_slots_from_calendar(
            seeded_session, tutor_id=1, start_date=start_date, end_date=end_date
        )
        
        assert len(busy_slots) == 2
        
        # First event: dateTime timezone-aware (Z -> UTC)
        assert busy_slots[0][0] == datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
        assert busy_slots[0][1] == datetime(2026, 5, 26, 13, 0, tzinfo=timezone.utc)
        
        # Second event: all-day date (parsed as midnight UTC)
        assert busy_slots[1][0] == datetime(2026, 5, 27, 0, 0, tzinfo=timezone.utc)
        assert busy_slots[1][1] == datetime(2026, 5, 28, 0, 0, tzinfo=timezone.utc)
