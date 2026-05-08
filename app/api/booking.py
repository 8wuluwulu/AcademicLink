"""
AcademicLink — Booking API Router

REST endpoints for creating and managing bookings.
Each request is scoped to a specific ``tutor_id`` (multi-tenant model).
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import Booking
from app.services.booking_service import create_booking_from_web

logger = logging.getLogger(__name__)

# ── Pydantic Schemas ─────────────────────────────────────────────────


class BookingCreate(BaseModel):
    """Request body for creating a new booking."""

    full_name: str = Field(
        ...,
        min_length=2,
        max_length=255,
        description="Student's full name",
        examples=["John Doe"],
    )
    phone: str = Field(
        ...,
        min_length=7,
        max_length=20,
        description="Student's contact phone number",
        examples=["+998901234567"],
    )
    service_type: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Type of tutoring service requested",
        examples=["IELTS Preparation"],
    )
    appointment_time: datetime = Field(
        ...,
        description="Desired appointment date and time",
        examples=["2026-05-15T14:00:00"],
    )
    tutor_id: int = Field(
        ...,
        gt=0,
        description="ID of the tutor to book a session with",
        examples=[1],
    )


class BookingRead(BaseModel):
    """Response body returned after a booking is created."""

    id: int = Field(..., description="Unique booking identifier")
    status: str = Field(
        default="success",
        description="Operation result status",
    )

    model_config = {"from_attributes": True}


# ── Notification Placeholder ─────────────────────────────────────────


async def notify_tutor_new_booking(booking: Booking) -> None:
    """
    Notify the assigned tutor about a new booking.

    TODO: Implement real notification via Telegram bot API.
          Send a formatted message to the tutor's ``tg_id`` with
          booking details (student name, service, appointment time).
    """
    logger.info(
        "PLACEHOLDER — would notify tutor_id=%d about booking #%d",
        booking.tutor_id,
        booking.id,
    )


# ── Router ───────────────────────────────────────────────────────────

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.post(
    "/",
    response_model=BookingRead,
    status_code=201,
    summary="Create a new booking",
    description="Submit a booking request for a specific tutor.",
)
async def create_booking(
    payload: BookingCreate,
    session: AsyncSession = Depends(get_session),
) -> BookingRead:
    """
    Create a booking for a student with a specific tutor.

    The endpoint validates the request body, delegates to the booking
    service, triggers a notification placeholder, and returns the
    created booking ID.
    """
    try:
        booking = await create_booking_from_web(
            session,
            full_name=payload.full_name,
            phone=payload.phone,
            service_type=payload.service_type,
            appointment_time=payload.appointment_time,
            tutor_id=payload.tutor_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Fire-and-forget notification (placeholder)
    await notify_tutor_new_booking(booking)

    return BookingRead(id=booking.id, status="success")
