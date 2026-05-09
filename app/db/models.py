"""
AcademicLink — Database Models

SQLModel table definitions for the tutor booking system.
Each class maps to a PostgreSQL table and doubles as a Pydantic schema.
"""

import enum
from datetime import datetime, time, timezone
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Text, Time
from sqlmodel import Field, Relationship, SQLModel


# ── Enums ────────────────────────────────────────────────────────────
class BookingStatus(str, enum.Enum):
    """Lifecycle states for a booking."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    COMPLETED = "COMPLETED"


# ── Student ──────────────────────────────────────────────────────────
class Student(SQLModel, table=True):
    """A student who books tutoring sessions."""

    __tablename__ = "students"

    id: int | None = Field(default=None, primary_key=True)
    full_name: str = Field(max_length=255, description="Student's full name")
    phone: str = Field(
        max_length=20,
        unique=True,
        index=True,
        description="Contact phone number (unique)",
    )
    telegram_id: int | None = Field(
        default=None,
        sa_type=BigInteger(),
        unique=True,
        index=True,
        description="Telegram user ID (optional)",
    )
    telegram_username: str | None = Field(
        default=None,
        max_length=32,
        description="Telegram @username (without @)",
    )
    notes: str | None = Field(
        default=None,
        sa_type=Text(),
        description="Tutor's private notes about the student",
    )
    prepaid_balance: int = Field(
        default=0,
        description="Number of pre-paid lessons remaining",
    )

    # ── Relationships ────────────────────────────────────────────────
    bookings: list["Booking"] = Relationship(back_populates="student")

    def __repr__(self) -> str:
        return f"<Student id={self.id} name={self.full_name!r}>"


# ── Tutor ────────────────────────────────────────────────────────────
class Tutor(SQLModel, table=True):
    """A tutor who provides academic services."""

    __tablename__ = "tutors"

    id: int | None = Field(default=None, primary_key=True)
    tg_id: int = Field(
        sa_type=BigInteger(),
        unique=True,
        index=True,
        description="Tutor's Telegram ID (for the admin bot)",
    )
    name: str = Field(max_length=255, description="Tutor's display name")
    is_active: bool = Field(
        default=True,
        description="Whether the tutor is currently accepting bookings",
    )

    # ── Relationships ────────────────────────────────────────────────
    bookings: list["Booking"] = Relationship(back_populates="tutor")
    availability_slots: list["AvailabilitySlot"] = Relationship(
        back_populates="tutor",
    )

    def __repr__(self) -> str:
        return f"<Tutor id={self.id} name={self.name!r} active={self.is_active}>"


# ── AvailabilitySlot ─────────────────────────────────────────────────
class AvailabilitySlot(SQLModel, table=True):
    """A recurring weekly time window when a tutor is available."""

    __tablename__ = "availability_slots"

    id: int | None = Field(default=None, primary_key=True)
    tutor_id: int = Field(foreign_key="tutors.id", index=True)
    weekday: int = Field(
        ge=0, le=6,
        description="Day of week: 0=Monday … 6=Sunday",
    )
    start_time: time = Field(
        sa_type=Time(),
        description="Slot start time (e.g. 09:00)",
    )
    end_time: time = Field(
        sa_type=Time(),
        description="Slot end time (e.g. 17:00)",
    )

    # ── Relationships ────────────────────────────────────────────────
    tutor: Optional[Tutor] = Relationship(back_populates="availability_slots")

    def __repr__(self) -> str:
        return (
            f"<AvailabilitySlot tutor={self.tutor_id} "
            f"day={self.weekday} {self.start_time}-{self.end_time}>"
        )


# ── Booking ──────────────────────────────────────────────────────────
class Booking(SQLModel, table=True):
    """A tutoring session booked by a student with a tutor."""

    __tablename__ = "bookings"

    id: int | None = Field(default=None, primary_key=True)

    # ── Foreign Keys ─────────────────────────────────────────────────
    student_id: int = Field(foreign_key="students.id", index=True)
    tutor_id: int = Field(foreign_key="tutors.id", index=True)

    # ── Booking Details ──────────────────────────────────────────────
    service_type: str = Field(
        max_length=100,
        description='Type of service, e.g. "IELTS Preparation"',
    )
    appointment_time: datetime = Field(
        sa_type=DateTime(timezone=True),
        description="Scheduled date/time for the session",
    )
    status: BookingStatus = Field(
        default=BookingStatus.PENDING,
        description="Current booking lifecycle state",
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_type=DateTime(timezone=True),
        description="Record creation timestamp (UTC)",
    )
    reminded_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        description="When the pre-lesson reminder was sent (NULL = not sent)",
    )
    followed_up_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        description="When the post-lesson follow-up was sent (NULL = not sent)",
    )

    # ── Relationships ────────────────────────────────────────────────
    student: Optional[Student] = Relationship(back_populates="bookings")
    tutor: Optional[Tutor] = Relationship(back_populates="bookings")

    def __repr__(self) -> str:
        return (
            f"<Booking id={self.id} student={self.student_id} "
            f"tutor={self.tutor_id} status={self.status.value}>"
        )
