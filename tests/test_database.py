"""
AcademicLink — Unit Tests for app/db/database.py and migrations
"""

import pytest
import pytest_asyncio
from sqlalchemy import text, inspect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import SQLModel

from app.db.database import get_session, init_db, _auto_migrate_columns, engine as db_engine


@pytest.mark.asyncio
async def test_get_session():
    generator = get_session()
    session = await anext(generator)
    assert isinstance(session, AsyncSession)
    await session.close()
    try:
        await anext(generator)
    except StopAsyncIteration:
        pass


@pytest.mark.asyncio
async def test_init_db():
    await init_db()


@pytest.mark.asyncio
async def test_auto_migrate_columns_tutors_and_bookings():
    temp_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    
    async with temp_engine.begin() as conn:
        await conn.execute(text("CREATE TABLE tutors (id INTEGER PRIMARY KEY, name TEXT, is_active BOOLEAN)"))
        await conn.execute(text("CREATE TABLE bookings (id INTEGER PRIMARY KEY, service_id INTEGER)"))
        
        def run_mig(connection):
            _auto_migrate_columns(connection)
            
        await conn.run_sync(run_mig)
        
        def check_cols(connection):
            inspector = inspect(connection)
            tutors_cols = [c['name'] for c in inspector.get_columns('tutors')]
            assert 'google_token_json' in tutors_cols
            assert 'bio' in tutors_cols
            assert 'sbp_phone' in tutors_cols
            
            bookings_cols = [c['name'] for c in inspector.get_columns('bookings')]
            assert 'payment_method' in bookings_cols
            assert 'google_event_id' in bookings_cols

        await conn.run_sync(check_cols)
        
    await temp_engine.dispose()


@pytest.mark.asyncio
async def test_auto_migrate_students_many_to_many_schema():
    temp_engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    
    async with temp_engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE students ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  tutor_id INTEGER,"
            "  full_name TEXT,"
            "  phone TEXT,"
            "  telegram_id INTEGER,"
            "  telegram_username TEXT,"
            "  notes TEXT,"
            "  prepaid_balance INTEGER,"
            "  is_active BOOLEAN,"
            "  wants_reminders BOOLEAN"
            ")"
        ))
        await conn.execute(text(
            "CREATE TABLE student_tutor_links ("
            "  student_id INTEGER,"
            "  tutor_id INTEGER,"
            "  prepaid_balance INTEGER,"
            "  notes TEXT,"
            "  is_active BOOLEAN,"
            "  PRIMARY KEY (student_id, tutor_id)"
            ")"
        ))
        await conn.execute(text(
            "CREATE TABLE bookings ("
            "  id INTEGER PRIMARY KEY,"
            "  student_id INTEGER"
            ")"
        ))
        
        await conn.execute(text(
            "INSERT INTO students (tutor_id, full_name, phone, telegram_id, telegram_username, notes, prepaid_balance, is_active, wants_reminders) "
            "VALUES (1, 'Duplicate Student', '+79001112233', 12345, 'dup_uname', 'notes for tutor 1', 100, 1, 1)"
        ))
        await conn.execute(text(
            "INSERT INTO students (tutor_id, full_name, phone, telegram_id, telegram_username, notes, prepaid_balance, is_active, wants_reminders) "
            "VALUES (2, 'Duplicate Student', '+79001112233', 12345, 'dup_uname', 'notes for tutor 2', 200, 1, 1)"
        ))
        
        await conn.execute(text("INSERT INTO bookings (id, student_id) VALUES (100, 2)"))
        
        def run_mig(connection):
            orig_execute = connection.execute
            
            def mock_execute(statement, *args, **kwargs):
                stmt_str = str(statement)
                if "DROP CONSTRAINT" in stmt_str or "DROP COLUMN" in stmt_str or "ADD CONSTRAINT" in stmt_str:
                    # SQLite doesn't support DROP CONSTRAINT / DROP COLUMN / ADD CONSTRAINT
                    return None
                return orig_execute(statement, *args, **kwargs)
                
            connection.execute = mock_execute
            _auto_migrate_columns(connection)
            
        await conn.run_sync(run_mig)
        
        res_students = (await conn.execute(text("SELECT id FROM students"))).all()
        assert len(res_students) == 1
        assert res_students[0][0] == 1
        
        res_bookings = (await conn.execute(text("SELECT student_id FROM bookings WHERE id=100"))).scalar()
        assert res_bookings == 1
        
        res_links = (await conn.execute(text("SELECT tutor_id, prepaid_balance, notes FROM student_tutor_links"))).all()
        assert len(res_links) == 2
        res_links.sort(key=lambda r: r[0])
        assert res_links[0] == (1, 100, "notes for tutor 1")
        assert res_links[1] == (2, 200, "notes for tutor 2")

    await temp_engine.dispose()


def test_engine_shim():
    from app.db.engine import engine, async_session_factory, get_session, init_db
    assert engine is not None
    assert async_session_factory is not None
    assert get_session is not None
    assert init_db is not None


def test_models_repr():
    from app.db.models import Student, Tutor, Service, AvailabilitySlot, TutorAbsence, Booking, BookingStatus
    from datetime import time, datetime, timezone
    
    student = Student(id=1, full_name="John Doe", phone="+12345")
    assert "John Doe" in repr(student)
    
    tutor = Tutor(id=2, name="Jane Tutor", tg_id=123)
    assert "Jane Tutor" in repr(tutor)
    
    service = Service(name="Prep", duration=60)
    assert "Prep" in repr(service)
    
    slot = AvailabilitySlot(tutor_id=2, weekday=1, start_time=time(9, 0), end_time=time(10, 0))
    assert "9:00" in repr(slot) or "09:00" in repr(slot)
    
    absence = TutorAbsence(tutor_id=2, start_time=datetime.now(timezone.utc), end_time=datetime.now(timezone.utc))
    assert "TutorAbsence" in repr(absence)
    
    booking = Booking(id=5, student_id=1, tutor_id=2, status=BookingStatus.PENDING, service_type="Prep", appointment_time=datetime.now())
    assert "Booking" in repr(booking)


def test_bot_core():
    from app.core.bot import set_bot, set_bot_username, get_bot_username, get_bot
    from aiogram import Bot
    
    mock_bot = Bot(token="12345678:ABCDEF1234567890ABCDEF1234567890ABC")
    set_bot(mock_bot)
    assert get_bot() == mock_bot
    
    set_bot_username("my_test_bot")
    assert get_bot_username() == "my_test_bot"
    
    from app.core import bot as bot_module
    bot_module._bot_username = None
    assert get_bot_username() == "bot"


@pytest.mark.asyncio
async def test_health_check():
    from app.api.router import health_check
    res = await health_check()
    assert res == {"status": "ok"}


def test_settings_properties():
    from app.core.config import settings
    assert settings.is_testing is True
    assert settings.is_production is False
