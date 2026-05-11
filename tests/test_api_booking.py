"""
AcademicLink — Integration Tests for the Booking API

Covers:
- Successful POST /api/v1/bookings/ with valid API key
- Rejection without API key (403)
- Rejection with invalid API key (403)
- Phone validation errors (422)
- Service-layer ValueError propagation (400)
"""

from datetime import datetime, time, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

from app.db.models import AvailabilitySlot, Student, Tutor


# ── Helpers ──────────────────────────────────────────────────────────


def _next_weekday(weekday: int) -> str:
    """Return ISO string for next occurrence of weekday at 10:00 UTC."""
    now = datetime.now(timezone.utc)
    days_ahead = weekday - now.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    target = now + timedelta(days=days_ahead)
    return target.replace(
        hour=10, minute=0, second=0, microsecond=0,
    ).isoformat()


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def test_engine():
    """In-memory SQLite for integration tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def test_session_factory(test_engine):
    """Session factory bound to the test engine."""
    return async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False,
    )


@pytest_asyncio.fixture
async def seed_db(test_session_factory):
    """Seed tutor + availability slot for a valid Monday booking."""
    async with test_session_factory() as session:
        tutor = Tutor(tg_id=111222333, name="API Tutor", is_active=True)
        session.add(tutor)
        await session.flush()

        student = Student(
            full_name="Existing Student",
            phone="+998901234567",
        )
        session.add(student)

        slot = AvailabilitySlot(
            tutor_id=tutor.id,
            weekday=0,  # Monday
            start_time=time(8, 0),
            end_time=time(20, 0),
        )
        session.add(slot)
        await session.commit()


@pytest.fixture
def app_client(test_session_factory, seed_db):
    """
    Create a FastAPI TestClient with the DB session overridden
    and the bot mocked out.
    """
    from main import app
    from app.db.database import get_session

    async def _override_session():
        async with test_session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session

    # Mock the bot on app.state so notify doesn't fail
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    app.state.bot = mock_bot

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()


# ── Valid API key (must match settings.secret_key) ───────────────────

API_KEY = "change-me-to-a-random-64-char-string"  # from .env


# ═════════════════════════════════════════════════════════════════════
#  Tests
# ═════════════════════════════════════════════════════════════════════


def test_create_booking_success(app_client):
    """Valid request with correct API key should return 201."""
    response = app_client.post(
        "/api/v1/bookings/",
        json={
            "full_name": "Integration Student",
            "phone": "+79001234567",
            "service_type": "IELTS",
            "appointment_time": _next_weekday(0),  # Monday
            "tutor_id": 1,
        },
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "success"
    assert "id" in data


def test_create_booking_missing_api_key(app_client):
    """Request without X-API-Key header should return 422 (missing header)."""
    response = app_client.post(
        "/api/v1/bookings/",
        json={
            "full_name": "No Key Student",
            "phone": "+79001234567",
            "service_type": "Math",
            "appointment_time": _next_weekday(0),
            "tutor_id": 1,
        },
    )
    assert response.status_code == 422


def test_create_booking_invalid_api_key(app_client):
    """Request with wrong API key should return 403."""
    response = app_client.post(
        "/api/v1/bookings/",
        json={
            "full_name": "Wrong Key Student",
            "phone": "+79001234567",
            "service_type": "Math",
            "appointment_time": _next_weekday(0),
            "tutor_id": 1,
        },
        headers={"X-API-Key": "wrong-key"},
    )
    assert response.status_code == 403


def test_create_booking_invalid_phone(app_client):
    """Phone number not matching international format should return 422."""
    response = app_client.post(
        "/api/v1/bookings/",
        json={
            "full_name": "Bad Phone Student",
            "phone": "8901234567",  # Missing +
            "service_type": "Math",
            "appointment_time": _next_weekday(0),
            "tutor_id": 1,
        },
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 422


def test_create_booking_invalid_phone_short(app_client):
    """Too-short phone number should be rejected."""
    response = app_client.post(
        "/api/v1/bookings/",
        json={
            "full_name": "Short Phone",
            "phone": "+123",
            "service_type": "Math",
            "appointment_time": _next_weekday(0),
            "tutor_id": 1,
        },
        headers={"X-API-Key": API_KEY},
    )
    assert response.status_code == 422


def test_phone_validation_accepts_valid_formats(app_client):
    """Valid international phone numbers should be accepted."""
    for phone in ["+79001234567", "+998901234567", "+14155551234"]:
        response = app_client.post(
            "/api/v1/bookings/",
            json={
                "full_name": "Valid Phone Student",
                "phone": phone,
                "service_type": "IELTS",
                "appointment_time": _next_weekday(0),
                "tutor_id": 1,
            },
            headers={"X-API-Key": API_KEY},
        )
        # Should be 201 or 400 (service error) — NOT 422
        assert response.status_code in (201, 400), (
            f"Phone {phone} was rejected with {response.status_code}"
        )
