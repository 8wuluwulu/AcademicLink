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
async def test_sync_booking_pending_no_event(seeded_session: AsyncSession):
    """If booking status is PENDING and there is no google_event_id, it should exit silently without requests."""
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
        appointment_time=datetime.now(timezone.utc),
        status=BookingStatus.PENDING,
        google_event_id=None,
    )

    with patch("httpx.AsyncClient.request") as mock_request:
        await sync_booking_to_calendar(seeded_session, booking)
        mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_sync_booking_non_confirmed_with_event_deletes(seeded_session: AsyncSession):
    """If booking status is CANCELLED but google_event_id is set, it should delete the event."""
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
        appointment_time=datetime.now(timezone.utc),
        status=BookingStatus.CANCELLED,
        google_event_id="google_event_12345",
    )
    seeded_session.add(booking)
    await seeded_session.flush()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200

    with patch("httpx.AsyncClient.request", return_value=mock_response) as mock_request:
        await sync_booking_to_calendar(seeded_session, booking)
        
        # Verify a DELETE request was made
        mock_request.assert_called_once()
        method, url = mock_request.call_args[0]
        assert method == "DELETE"
        assert "events/google_event_12345" in url
        
        # Verify the event ID was cleared in DB
        assert booking.google_event_id is None


@pytest.mark.asyncio
async def test_sync_booking_new_event(seeded_session: AsyncSession):
    """If no google_event_id is set, a POST request should create the event and save its ID."""
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({
        "access_token": "mock_access_token",
        "refresh_token": "mock_refresh_token"
    })
    tutor.meeting_link = "https://zoom.us/my-room"
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


# ── Additional Tests for 100% Coverage ────────────────────────────────

from app.services.google_calendar_service import (
    _get_access_token,
    _refresh_access_token,
    _execute_google_request,
)
from app.core.config import settings

@pytest.mark.asyncio
async def test_get_access_token_invalid_json(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = "invalid-json"
    await seeded_session.commit()
    token = await _get_access_token(tutor, seeded_session)
    assert token is None

@pytest.mark.asyncio
async def test_get_access_token_missing_access_token(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"refresh_token": "abc"})
    await seeded_session.commit()
    token = await _get_access_token(tutor, seeded_session)
    assert token is None

@pytest.mark.asyncio
async def test_refresh_access_token_invalid_json(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = "invalid-json"
    await seeded_session.commit()
    token = await _refresh_access_token(tutor, seeded_session)
    assert token is None

@pytest.mark.asyncio
async def test_refresh_access_token_none_json(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = None
    await seeded_session.commit()
    token = await _refresh_access_token(tutor, seeded_session)
    assert token is None

@pytest.mark.asyncio
async def test_refresh_access_token_no_refresh_token(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc"})
    await seeded_session.commit()
    token = await _refresh_access_token(tutor, seeded_session)
    assert token is None

@pytest.mark.asyncio
async def test_refresh_access_token_no_credentials(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"refresh_token": "abc"})
    await seeded_session.commit()
    with patch.object(settings, "google_client_id", None):
        token = await _refresh_access_token(tutor, seeded_session)
        assert token is None

@pytest.mark.asyncio
async def test_refresh_access_token_request_fails(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"refresh_token": "abc"})
    await seeded_session.commit()
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 400
    mock_response.text = "Bad request"
    with patch.object(settings, "google_client_id", "some_id"), \
         patch.object(settings, "google_client_secret", "some_secret"), \
         patch("httpx.AsyncClient.post", return_value=mock_response):
        token = await _refresh_access_token(tutor, seeded_session)
        assert token is None

@pytest.mark.asyncio
async def test_refresh_access_token_request_exception(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"refresh_token": "abc"})
    await seeded_session.commit()
    with patch.object(settings, "google_client_id", "some_id"), \
         patch.object(settings, "google_client_secret", "some_secret"), \
         patch("httpx.AsyncClient.post", side_effect=httpx.RequestError("Error")):
        token = await _refresh_access_token(tutor, seeded_session)
        assert token is None

@pytest.mark.asyncio
async def test_refresh_access_token_request_success(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"refresh_token": "abc", "access_token": "old_token"})
    await seeded_session.commit()
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {"access_token": "new_token", "expires_in": 3600}
    with patch.object(settings, "google_client_id", "some_id"), \
         patch.object(settings, "google_client_secret", "some_secret"), \
         patch("httpx.AsyncClient.post", return_value=mock_response):
        token = await _refresh_access_token(tutor, seeded_session)
        assert token == "new_token"
        await seeded_session.refresh(tutor)
        tokens_in_db = json.loads(tutor.google_token_json)
        assert tokens_in_db["access_token"] == "new_token"
        assert tokens_in_db["refresh_token"] == "abc"

@pytest.mark.asyncio
async def test_execute_google_request_no_token(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = None
    await seeded_session.commit()
    res = await _execute_google_request(tutor, seeded_session, "GET", "url")
    assert res is None

@pytest.mark.asyncio
async def test_execute_google_request_exception(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc"})
    await seeded_session.commit()
    with patch("httpx.AsyncClient.request", side_effect=Exception("HTTP Error")):
        res = await _execute_google_request(tutor, seeded_session, "GET", "url")
        assert res is None

@pytest.mark.asyncio
async def test_execute_google_request_401_retry_success(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc", "refresh_token": "xyz"})
    await seeded_session.commit()

    mock_resp_401 = MagicMock(spec=httpx.Response)
    mock_resp_401.status_code = 401
    mock_resp_200 = MagicMock(spec=httpx.Response)
    mock_resp_200.status_code = 200

    with patch("httpx.AsyncClient.request", side_effect=[mock_resp_401, mock_resp_200]) as mock_request, \
         patch("app.services.google_calendar_service._refresh_access_token", return_value="new_token") as mock_refresh:
        res = await _execute_google_request(tutor, seeded_session, "GET", "url")
        assert res == mock_resp_200
        mock_refresh.assert_called_once_with(tutor, seeded_session)
        assert mock_request.call_count == 2

@pytest.mark.asyncio
async def test_sync_booking_tutor_not_found(seeded_session: AsyncSession):
    booking = Booking(
        id=999,
        tutor_id=9999,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime.now(timezone.utc),
        status=BookingStatus.CONFIRMED,
    )
    with patch("app.services.google_calendar_service._execute_google_request") as mock_req:
        await sync_booking_to_calendar(seeded_session, booking)
        mock_req.assert_not_called()

@pytest.mark.asyncio
async def test_sync_booking_update_404_recreates(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc"})
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

    mock_resp_404 = MagicMock(spec=httpx.Response)
    mock_resp_404.status_code = 404
    mock_resp_200 = MagicMock(spec=httpx.Response)
    mock_resp_200.status_code = 200
    mock_resp_200.json.return_value = {"id": "google_event_new"}

    with patch("app.services.google_calendar_service._execute_google_request", side_effect=[mock_resp_404, mock_resp_200]):
        await sync_booking_to_calendar(seeded_session, booking)
        assert booking.google_event_id == "google_event_new"

@pytest.mark.asyncio
async def test_sync_booking_update_failed_request(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc"})
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

    mock_resp_500 = MagicMock(spec=httpx.Response)
    mock_resp_500.status_code = 500
    mock_resp_500.text = "Internal Server Error"

    with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_resp_500):
        await sync_booking_to_calendar(seeded_session, booking)
        assert booking.google_event_id == "google_event_12345"

@pytest.mark.asyncio
async def test_sync_booking_create_failed_request(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc"})
    await seeded_session.commit()

    booking = Booking(
        id=999,
        tutor_id=1,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
        status=BookingStatus.CONFIRMED,
        google_event_id=None,
    )
    seeded_session.add(booking)
    await seeded_session.flush()

    mock_resp_500 = MagicMock(spec=httpx.Response)
    mock_resp_500.status_code = 500
    mock_resp_500.text = "Internal Server Error"

    with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_resp_500):
        await sync_booking_to_calendar(seeded_session, booking)
        assert booking.google_event_id is None

@pytest.mark.asyncio
async def test_delete_calendar_event_no_id(seeded_session: AsyncSession):
    booking = Booking(
        id=999,
        tutor_id=1,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime.now(timezone.utc),
        status=BookingStatus.CANCELLED,
        google_event_id=None,
    )
    with patch("app.services.google_calendar_service._execute_google_request") as mock_req:
        await delete_calendar_event(seeded_session, booking)
        mock_req.assert_not_called()

@pytest.mark.asyncio
async def test_delete_calendar_event_tutor_not_found(seeded_session: AsyncSession):
    booking = Booking(
        id=999,
        tutor_id=9999,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime.now(timezone.utc),
        status=BookingStatus.CANCELLED,
        google_event_id="google_event_12345",
    )
    with patch("app.services.google_calendar_service._execute_google_request") as mock_req:
        await delete_calendar_event(seeded_session, booking)
        mock_req.assert_not_called()

@pytest.mark.asyncio
async def test_delete_calendar_event_404_already_deleted(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc"})
    await seeded_session.commit()

    booking = Booking(
        id=999,
        tutor_id=1,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime.now(timezone.utc),
        status=BookingStatus.CANCELLED,
        google_event_id="google_event_12345",
    )
    seeded_session.add(booking)
    await seeded_session.flush()

    mock_resp_404 = MagicMock(spec=httpx.Response)
    mock_resp_404.status_code = 404

    with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_resp_404):
        await delete_calendar_event(seeded_session, booking)
        assert booking.google_event_id is None

@pytest.mark.asyncio
async def test_delete_calendar_event_failed_request(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc"})
    await seeded_session.commit()

    booking = Booking(
        id=999,
        tutor_id=1,
        student_id=1,
        service_id=1,
        service_type="Индивидуальный урок",
        appointment_time=datetime.now(timezone.utc),
        status=BookingStatus.CANCELLED,
        google_event_id="google_event_12345",
    )
    seeded_session.add(booking)
    await seeded_session.flush()

    mock_resp_500 = MagicMock(spec=httpx.Response)
    mock_resp_500.status_code = 500
    mock_resp_500.text = "Internal Server Error"

    with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_resp_500):
        await delete_calendar_event(seeded_session, booking)
        assert booking.google_event_id == "google_event_12345"

@pytest.mark.asyncio
async def test_get_busy_slots_tutor_not_found(seeded_session: AsyncSession):
    start_date = datetime(2026, 5, 25, tzinfo=timezone.utc)
    end_date = datetime(2026, 5, 29, tzinfo=timezone.utc)
    slots = await get_busy_slots_from_calendar(seeded_session, tutor_id=9999, start_date=start_date, end_date=end_date)
    assert slots == []

@pytest.mark.asyncio
async def test_get_busy_slots_failed_request(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc"})
    await seeded_session.commit()

    mock_resp_500 = MagicMock(spec=httpx.Response)
    mock_resp_500.status_code = 500

    start_date = datetime(2026, 5, 25, tzinfo=timezone.utc)
    end_date = datetime(2026, 5, 29, tzinfo=timezone.utc)

    with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_resp_500):
        slots = await get_busy_slots_from_calendar(seeded_session, tutor_id=1, start_date=start_date, end_date=end_date)
        assert slots == []

@pytest.mark.asyncio
async def test_get_busy_slots_malformed_timestamps(seeded_session: AsyncSession):
    tutor = await seeded_session.get(Tutor, 1)
    tutor.google_token_json = json.dumps({"access_token": "abc"})
    await seeded_session.commit()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "items": [
            {
                "start": {"dateTime": "2026-05-26T12:00:00Z"}
            },
            {
                "start": {"dateTime": "invalid-dateTime-format"},
                "end": {"dateTime": "invalid-dateTime-format"}
            },
            {
                "start": {"dateTime": "2026-05-26T14:00:00Z"},
                "end": {"dateTime": "2026-05-26T15:00:00Z"}
            }
        ]
    }

    start_date = datetime(2026, 5, 25, tzinfo=timezone.utc)
    end_date = datetime(2026, 5, 29, tzinfo=timezone.utc)

    with patch("app.services.google_calendar_service._execute_google_request", return_value=mock_response):
        slots = await get_busy_slots_from_calendar(seeded_session, tutor_id=1, start_date=start_date, end_date=end_date)
        assert len(slots) == 1
        assert slots[0][0] == datetime(2026, 5, 26, 14, 0, tzinfo=timezone.utc)
        assert slots[0][1] == datetime(2026, 5, 26, 15, 0, tzinfo=timezone.utc)
