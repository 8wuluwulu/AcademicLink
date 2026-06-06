"""
AcademicLink — Tutor API Router

Endpoints for fetching tutor information, services, and availability slots.
"""

import logging
from datetime import date as dt_date, datetime, time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_session
from app.db.models import Tutor, Service, Student, StudentTutorLink
from app.services.booking_service import get_available_slots

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tutors", tags=["Tutors"])


# ── Pydantic Schemas ─────────────────────────────────────────────────

class TutorRead(BaseModel):
    id: int
    name: str
    is_active: bool
    meeting_link: str | None = None
    bio: str | None = None
    subject: str | None = None
    avatar_url: str | None = None
    accent_color: str = "#4f46e5"
    sbp_phone: str | None = None
    sbp_bank: str | None = None
    sbp_qr_url: str | None = None
    sbp_link: str | None = None

    model_config = {"from_attributes": True}


class ServiceRead(BaseModel):
    id: int
    name: str
    duration: int
    buffer_time: int
    price: int | None
    is_active: bool

    model_config = {"from_attributes": True}


class SlotsResponse(BaseModel):
    date: dt_date
    tutor_id: int
    available_slots: list[str] = Field(..., description="List of HH:MM start times")


# ── Routes ───────────────────────────────────────────────────────────

@router.get("/", response_model=list[TutorRead])
async def list_tutors(session: AsyncSession = Depends(get_session)):
    """List all active tutors."""
    result = await session.execute(select(Tutor).where(Tutor.is_active == True))
    return result.scalars().all()


@router.get("/by-student", response_model=list[TutorRead])
async def get_tutors_by_student(
    telegram_id: int | None = Query(None, description="Telegram ID of the student"),
    phone: str | None = Query(None, description="Phone number of the student"),
    session: AsyncSession = Depends(get_session),
) -> list[TutorRead]:
    """Get list of active tutors associated with a student."""
    if not telegram_id and not phone:
        return []
    
    stmt = select(Tutor)
    if telegram_id:
        stmt = stmt.join(StudentTutorLink, StudentTutorLink.tutor_id == Tutor.id).join(Student, Student.id == StudentTutorLink.student_id).where(Student.telegram_id == telegram_id)
    elif phone:
        digits = "".join(c for c in phone if c.isdigit())
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        elif len(digits) == 10:
            digits = "7" + digits
        normalized = f"+{digits}"
        
        stmt = stmt.join(StudentTutorLink, StudentTutorLink.tutor_id == Tutor.id).join(Student, Student.id == StudentTutorLink.student_id).where(
            (Student.phone == phone) | (Student.phone == normalized) | (Student.phone == digits)
        )
        
    stmt = stmt.where(Tutor.is_active == True)
    result = await session.execute(stmt)
    tutors = result.scalars().all()
    return tutors


@router.get("/{tutor_id}", response_model=TutorRead)
async def get_tutor(tutor_id: int, session: AsyncSession = Depends(get_session)):
    """Get details for a specific tutor."""
    tutor = await session.get(Tutor, tutor_id)
    if not tutor:
        raise HTTPException(status_code=404, detail="Tutor not found")
    return tutor


@router.get("/{tutor_id}/services", response_model=list[ServiceRead])
async def list_tutor_services(tutor_id: int, session: AsyncSession = Depends(get_session)):
    """List all active services for a specific tutor."""
    result = await session.execute(
        select(Service).where(Service.tutor_id == tutor_id, Service.is_active == True)
    )
    return result.scalars().all()


@router.get("/{tutor_id}/slots", response_model=SlotsResponse)
async def get_tutor_slots(
    tutor_id: int,
    service_id: int = Query(..., description="ID of the service to check duration"),
    date: dt_date = Query(..., description="Date to check (YYYY-MM-DD)"),
    session: AsyncSession = Depends(get_session)
):
    """
    Get available booking slots for a tutor and service on a specific date.
    Returns a list of start times (HH:MM).
    """
    dt = datetime.combine(date, time.min)
    slots = await get_available_slots(session, tutor_id=tutor_id, service_id=service_id, date=dt)
    
    return SlotsResponse(
        date=date,
        tutor_id=tutor_id,
        available_slots=slots
    )
