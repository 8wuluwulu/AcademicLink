"""
AcademicLink — Database Models

SQLModel table definitions for the tutor booking system.
Each class maps to a PostgreSQL table and doubles as a Pydantic schema.
"""

import enum
from datetime import datetime, time, timezone
from typing import Optional

from sqlalchemy import BigInteger, DateTime, Text, Time
from sqlmodel import Field, Relationship, SQLModel, UniqueConstraint


# ── Enums ────────────────────────────────────────────────────────────
class BookingStatus(str, enum.Enum):
    """Lifecycle states for a booking."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    COMPLETED = "COMPLETED"


# ── StudentTutorLink ─────────────────────────────────────────────────
class StudentTutorLink(SQLModel, table=True):
    """Link table between Student and Tutor, with tutor-specific student information."""

    __tablename__ = "student_tutor_links"

    student_id: int = Field(
        foreign_key="students.id",
        primary_key=True,
        ondelete="CASCADE",
    )
    tutor_id: int = Field(
        foreign_key="tutors.id",
        primary_key=True,
        ondelete="CASCADE",
    )
    prepaid_balance: int = Field(
        default=0,
        description="Number of pre-paid lessons remaining with this tutor",
    )
    notes: str | None = Field(
        default=None,
        sa_type=Text(),
        description="Tutor's private notes about the student",
    )
    is_active: bool = Field(
        default=True,
        description="Whether this student is active for this tutor",
    )

    # ── Relationships ────────────────────────────────────────────────
    student: "Student" = Relationship(back_populates="tutor_links")
    tutor: "Tutor" = Relationship(back_populates="student_links")


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
        description="Contact phone number",
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
    wants_reminders: bool = Field(
        default=True,
        description="Whether the student wants reminder notifications",
    )

    # ── Relationships ────────────────────────────────────────────────
    bookings: list["Booking"] = Relationship(back_populates="student")
    tutor_links: list["StudentTutorLink"] = Relationship(
        back_populates="student",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

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
    lesson_duration: int = Field(
        default=60,
        description="Lesson duration in minutes (e.g. 45, 60, 90)",
    )
    buffer_time: int = Field(
        default=0,
        description="Buffer between lessons in minutes",
    )
    wants_reminders: bool = Field(
        default=True,
        description="Whether the tutor wants reminder notifications",
    )
    meeting_link: str | None = Field(
        default=None,
        max_length=512,
        description="Permanent Zoom/Meet link for lessons",
    )
    google_token_json: str | None = Field(
        default=None,
        description="Google OAuth tokens (JSON string)",
    )
    google_calendar_id: str | None = Field(
        default="primary",
        max_length=255,
        description="Google Calendar ID to sync bookings to",
    )
    subscription_expires_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        description="Subscription expiration timestamp (UTC)",
    )
    subscription_status: str | None = Field(
        default="trial",
        max_length=50,
        description="Subscription status: trial, active, or expired",
    )

    # ── Tutor Business Card (Landing Page) & SBP Details ─────────────
    bio: str | None = Field(
        default=None,
        sa_type=Text(),
        description="About the tutor, experience, credentials",
    )
    subject: str | None = Field(
        default=None,
        max_length=255,
        description="Subject / specialization (e.g. Mathematics, English)",
    )
    avatar_url: str | None = Field(
        default=None,
        max_length=512,
        description="URL of the tutor's photo / avatar",
    )
    accent_color: str = Field(
        default="#4f46e5",
        max_length=10,
        description="Hex code of the accent color for landing page custom theme",
    )
    sbp_phone: str | None = Field(
        default=None,
        max_length=20,
        description="Phone number registered in SBP for C2C transfers",
    )
    sbp_bank: str | None = Field(
        default=None,
        max_length=100,
        description="Name of the preferred bank for SBP transfer",
    )
    sbp_qr_url: str | None = Field(
        default=None,
        max_length=512,
        description="Direct image URL of static SBP QR code",
    )
    sbp_link: str | None = Field(
        default=None,
        max_length=512,
        description="URL for direct C2C payment/transfer (e.g. Tinkoff/Sberbank pay links)",
    )

    # ── Relationships ────────────────────────────────────────────────
    bookings: list["Booking"] = Relationship(back_populates="tutor")
    availability_slots: list["AvailabilitySlot"] = Relationship(
        back_populates="tutor",
    )
    services: list["Service"] = Relationship(back_populates="tutor")
    student_links: list["StudentTutorLink"] = Relationship(
        back_populates="tutor",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )

    def __repr__(self) -> str:
        return f"<Tutor id={self.id} name={self.name!r} active={self.is_active}>"


# ── Service ──────────────────────────────────────────────────────────
class Service(SQLModel, table=True):
    """A type of lesson offered by a tutor (e.g. 'IELTS Prep', 'Free Trial')."""

    __tablename__ = "services"

    id: int | None = Field(default=None, primary_key=True)
    tutor_id: int = Field(foreign_key="tutors.id", index=True)
    name: str = Field(max_length=100, description="Service name")
    duration: int = Field(default=60, description="Duration in minutes")
    buffer_time: int = Field(default=0, description="Buffer in minutes")
    price: int | None = Field(default=None, description="Price (optional)")
    is_active: bool = Field(default=True)

    # ── Relationships ────────────────────────────────────────────────
    tutor: Optional[Tutor] = Relationship(back_populates="services")

    def __repr__(self) -> str:
        return f"<Service {self.name!r} duration={self.duration}>"


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


# ── TutorAbsence ─────────────────────────────────────────────────────
class TutorAbsence(SQLModel, table=True):
    """A period when a tutor is unavailable (e.g. sick leave, vacation)."""

    __tablename__ = "tutor_absences"

    id: int | None = Field(default=None, primary_key=True)
    tutor_id: int = Field(foreign_key="tutors.id", index=True)

    start_time: datetime = Field(
        sa_type=DateTime(timezone=True),
        description="Absence start date/time (UTC)",
    )
    end_time: datetime = Field(
        sa_type=DateTime(timezone=True),
        description="Absence end date/time (UTC)",
    )
    reason: str | None = Field(
        default=None,
        sa_type=Text(),
        description="Optional reason for absence (e.g. 'Sick Leave')",
    )

    # ── Relationships ────────────────────────────────────────────────
    tutor: Optional[Tutor] = Relationship()

    def __repr__(self) -> str:
        return (
            f"<TutorAbsence tutor={self.tutor_id} "
            f"from={self.start_time} to={self.end_time}>"
        )


# ── Booking ──────────────────────────────────────────────────────────
class Booking(SQLModel, table=True):
    """A tutoring session booked by a student with a tutor."""

    __tablename__ = "bookings"

    id: int | None = Field(default=None, primary_key=True)

    # ── Foreign Keys ─────────────────────────────────────────────────
    student_id: int = Field(foreign_key="students.id", index=True)
    tutor_id: int = Field(foreign_key="tutors.id", index=True)
    service_id: int | None = Field(default=None, foreign_key="services.id", index=True)

    # ── Booking Details ──────────────────────────────────────────────
    service_type: str = Field(
        max_length=100,
        description='Type of service (snapshot of service name)',
    )
    student_name_snapshot: str | None = Field(default=None, max_length=255)
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
    reminded_24h_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
        description="When the 24h reminder was sent (NULL = not sent)",
    )
    payment_method: str = Field(
        default="cash",
        max_length=20,
        description="Payment method: cash, transfer, or online",
    )
    payment_comment: str | None = Field(
        default=None,
        sa_type=Text(),
        description="Verification metadata from payer (e.g. sender name for SBP transfer)",
    )
    google_event_id: str | None = Field(
        default=None,
        max_length=255,
        description="Google Calendar event ID mapped to this booking",
    )

    # ── Relationships ────────────────────────────────────────────────
    student: Optional[Student] = Relationship(back_populates="bookings")
    tutor: Optional[Tutor] = Relationship(back_populates="bookings")

    def __repr__(self) -> str:
        return (
            f"<Booking id={self.id} student={self.student_id} "
            f"tutor={self.tutor_id} status={self.status.value}>"
        )
