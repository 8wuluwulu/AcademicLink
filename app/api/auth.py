"""
AcademicLink — Google OAuth API Router

Endpoints for managing Google OAuth 2.0 authentication for tutors.
"""

import json
import logging
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_session
from app.db.models import Tutor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.get("/google/login/{tutor_id}")
async def google_login(tutor_id: int, session: AsyncSession = Depends(get_session)):
    """
    Redirect the tutor to the Google OAuth 2.0 consent screen.
    """
    tutor = await session.get(Tutor, tutor_id)
    if not tutor:
        raise HTTPException(status_code=404, detail="Tutor not found")

    if not settings.google_client_id:
        raise HTTPException(
            status_code=400,
            detail="Google Client ID is not configured in .env file."
        )

    redirect_uri = f"{settings.web_url}/api/v1/auth/google/callback"
    
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "https://www.googleapis.com/auth/calendar",
        "access_type": "offline",
        "prompt": "consent",
        "state": str(tutor_id),
    }
    
    authorization_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(authorization_url)


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    session: AsyncSession = Depends(get_session)
):
    """
    Handle the Google OAuth 2.0 redirect callback.
    Exchange the authorization code for access and refresh tokens.
    """
    if error:
        logger.error("Google OAuth error callback: %s", error)
        raise HTTPException(status_code=400, detail=f"Google OAuth failed: {error}")

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state in callback")

    try:
        tutor_id = int(state)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    tutor = await session.get(Tutor, tutor_id)
    if not tutor:
        raise HTTPException(status_code=404, detail="Tutor not found")

    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=500, detail="Google client credentials are not configured")

    redirect_uri = f"{settings.web_url}/api/v1/auth/google/callback"
    
    # Exchange code for tokens
    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=data)
        if response.status_code != 200:
            logger.error("Failed to exchange code: %s", response.text)
            raise HTTPException(status_code=400, detail="Failed to exchange authorization code for tokens")
        
        token_data = response.json()

    # Save tokens to Tutor
    tutor.google_token_json = json.dumps(token_data)
    await session.commit()
    logger.info("Google Calendar successfully connected for tutor id=%d", tutor.id)

    # Redirect user back to the Telegram bot
    bot = getattr(request.app.state, "bot", None)
    if bot:
        try:
            bot_info = await bot.get_me()
            return RedirectResponse(f"https://t.me/{bot_info.username}?start=gcal_success")
        except Exception as exc:
            logger.error("Failed to get bot info: %s", exc)

    return {"status": "success", "message": "Google Calendar connected! You can now return to the Telegram bot."}
