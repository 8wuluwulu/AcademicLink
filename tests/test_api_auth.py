"""
AcademicLink — Direct Unit Tests for Google OAuth Authentication API Endpoints
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.config import settings
from app.db.models import Tutor
from app.api.auth import google_login, google_callback


@pytest_asyncio.fixture
async def auth_engine():
    """In-memory SQLite for auth tests."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def auth_session_factory(auth_engine):
    """Session factory for auth tests."""
    return async_sessionmaker(
        auth_engine, class_=AsyncSession, expire_on_commit=False
    )


@pytest_asyncio.fixture
async def db_session(auth_session_factory):
    async with auth_session_factory() as session:
        tutor = Tutor(
            id=10,
            tg_id=55555,
            name="OAuth Tutor",
            is_active=True,
            subscription_status="active",
        )
        session.add(tutor)
        await session.commit()
        
    async with auth_session_factory() as session:
        yield session


@pytest.mark.asyncio
async def test_google_login_not_found(db_session):
    with pytest.raises(HTTPException) as exc_info:
        await google_login(tutor_id=999, session=db_session)
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Tutor not found"


@pytest.mark.asyncio
async def test_google_login_missing_client_id(db_session):
    with patch.object(settings, "google_client_id", None):
        with pytest.raises(HTTPException) as exc_info:
            await google_login(tutor_id=10, session=db_session)
        assert exc_info.value.status_code == 400
        assert "Google Client ID is not configured" in exc_info.value.detail


@pytest.mark.asyncio
async def test_google_login_success(db_session):
    with patch.object(settings, "google_client_id", "test-client-id"), \
         patch.object(settings, "web_url", "https://academic.link"):
        
        response = await google_login(tutor_id=10, session=db_session)
        assert isinstance(response, RedirectResponse)
        assert response.status_code in (302, 307)
        location = response.headers["location"]
        assert "accounts.google.com" in location
        assert "client_id=test-client-id" in location
        assert "state=10" in location


@pytest.mark.asyncio
async def test_google_callback_error(db_session):
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc_info:
        await google_callback(request=mock_request, error="access_denied", session=db_session)
    assert exc_info.value.status_code == 400
    assert "Google OAuth failed: access_denied" in exc_info.value.detail


@pytest.mark.asyncio
async def test_google_callback_missing_params(db_session):
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc_info:
        await google_callback(request=mock_request, code="123", session=db_session)
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        await google_callback(request=mock_request, state="10", session=db_session)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_google_callback_invalid_state(db_session):
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc_info:
        await google_callback(request=mock_request, code="123", state="not-an-int", session=db_session)
    assert exc_info.value.status_code == 400
    assert "Invalid state parameter" in exc_info.value.detail


@pytest.mark.asyncio
async def test_google_callback_tutor_not_found(db_session):
    mock_request = MagicMock(spec=Request)
    with pytest.raises(HTTPException) as exc_info:
        await google_callback(request=mock_request, code="123", state="999", session=db_session)
    assert exc_info.value.status_code == 404
    assert "Tutor not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_google_callback_missing_config(db_session):
    mock_request = MagicMock(spec=Request)
    with patch.object(settings, "google_client_id", None):
        with pytest.raises(HTTPException) as exc_info:
            await google_callback(request=mock_request, code="123", state="10", session=db_session)
        assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_google_callback_exchange_failed(db_session):
    mock_request = MagicMock(spec=Request)
    with patch.object(settings, "google_client_id", "id"), \
         patch.object(settings, "google_client_secret", "secret"), \
         patch("httpx.AsyncClient.post") as mock_post:
        
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "invalid_grant"
        mock_post.return_value = mock_response

        with pytest.raises(HTTPException) as exc_info:
            await google_callback(request=mock_request, code="auth_code", state="10", session=db_session)
        assert exc_info.value.status_code == 400
        assert "Failed to exchange authorization code" in exc_info.value.detail


@pytest.mark.asyncio
async def test_google_callback_success(db_session):
    mock_request = MagicMock(spec=Request)
    mock_request.app = MagicMock()
    mock_request.app.state = MagicMock()
    
    # Ensure no bot exists on state to fall back to JSON response
    if hasattr(mock_request.app.state, "bot"):
        delattr(mock_request.app.state, "bot")

    with patch.object(settings, "google_client_id", "id"), \
         patch.object(settings, "google_client_secret", "secret"), \
         patch("httpx.AsyncClient.post") as mock_post:
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "abc", "refresh_token": "xyz"}
        mock_post.return_value = mock_response

        response = await google_callback(request=mock_request, code="auth_code", state="10", session=db_session)
        assert response == {"status": "success", "message": "Google Calendar connected! You can now return to the Telegram bot."}

        # Check DB updated
        tutor = await db_session.get(Tutor, 10)
        assert tutor.google_token_json is not None
        token_data = json.loads(tutor.google_token_json)
        assert token_data["access_token"] == "abc"
        assert token_data["refresh_token"] == "xyz"


@pytest.mark.asyncio
async def test_google_callback_success_redirect_bot(db_session):
    mock_request = MagicMock(spec=Request)
    mock_request.app = MagicMock()
    mock_request.app.state = MagicMock()
    
    mock_bot = AsyncMock()
    mock_bot.get_me.return_value = MagicMock(username="my_tutor_bot")
    mock_request.app.state.bot = mock_bot

    with patch.object(settings, "google_client_id", "id"), \
         patch.object(settings, "google_client_secret", "secret"), \
         patch("httpx.AsyncClient.post") as mock_post:
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "abc"}
        mock_post.return_value = mock_response

        response = await google_callback(request=mock_request, code="auth_code", state="10", session=db_session)
        assert isinstance(response, RedirectResponse)
        assert response.status_code in (302, 307)
        assert response.headers["location"] == "https://t.me/my_tutor_bot?start=gcal_success"


@pytest.mark.asyncio
async def test_google_callback_success_bot_exception(db_session):
    """If bot get_me raises Exception, it should fall back to JSON success message instead of raising."""
    mock_request = MagicMock(spec=Request)
    mock_request.app = MagicMock()
    mock_request.app.state = MagicMock()
    
    mock_bot = AsyncMock()
    mock_bot.get_me.side_effect = Exception("Bot API down")
    mock_request.app.state.bot = mock_bot

    with patch.object(settings, "google_client_id", "id"), \
         patch.object(settings, "google_client_secret", "secret"), \
         patch("httpx.AsyncClient.post") as mock_post:
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "abc"}
        mock_post.return_value = mock_response

        response = await google_callback(request=mock_request, code="auth_code", state="10", session=db_session)
        assert response == {"status": "success", "message": "Google Calendar connected! You can now return to the Telegram bot."}
