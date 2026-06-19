"""
AcademicLink — Tests for Tutor Service
"""

from unittest.mock import patch
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Tutor, Service, AvailabilitySlot
from app.services.tutor_service import ensure_tutor_exists


@pytest.mark.asyncio
async def test_ensure_tutor_exists_when_tutor_already_active(session: AsyncSession):
    """If an active tutor exists, return it and do not seed a new one."""
    tutor = Tutor(tg_id=999, name="Active Tutor", is_active=True)
    session.add(tutor)
    await session.commit()

    # Call service
    result = await ensure_tutor_exists(session)
    
    assert result is not None
    assert result.tg_id == 999
    assert result.name == "Active Tutor"


@pytest.mark.asyncio
async def test_ensure_tutor_exists_when_tutor_inactive(session: AsyncSession):
    """If only inactive tutors exist, return the first one."""
    tutor1 = Tutor(tg_id=111, name="Inactive 1", is_active=False)
    tutor2 = Tutor(tg_id=222, name="Inactive 2", is_active=False)
    session.add_all([tutor1, tutor2])
    await session.commit()

    result = await ensure_tutor_exists(session)
    
    assert result is not None
    # Depending on order, it should return the first one (id-based or just order-based)
    assert result.is_active is False


@pytest.mark.asyncio
async def test_ensure_tutor_exists_no_tutors_no_default_id(session: AsyncSession):
    """If no tutors exist and default TG ID is None, skip seeding and return None."""
    with patch.object(settings, "default_tutor_tg_id", None):
        result = await ensure_tutor_exists(session)
        assert result is None


@pytest.mark.asyncio
async def test_ensure_tutor_exists_seeds_default(session: AsyncSession):
    """If no tutors exist, seed default tutor, services, and availability slots."""
    with patch.object(settings, "default_tutor_tg_id", 8888), \
         patch.object(settings, "default_tutor_name", "Seeded Tutor"):
        
        result = await ensure_tutor_exists(session)
        
        assert result is not None
        assert result.tg_id == 8888
        assert result.name == "Seeded Tutor"
        assert result.is_active is True

        # Verify default service created
        services_res = await session.execute(select(Service).where(Service.tutor_id == result.id))
        services = services_res.scalars().all()
        assert len(services) == 1
        assert services[0].name == "Занятие по математике"
        assert services[0].duration == 60
        assert services[0].buffer_time == 15
        assert services[0].price == 1500

        # Verify availability slots created (5 days: Mon-Fri)
        slots_res = await session.execute(select(AvailabilitySlot).where(AvailabilitySlot.tutor_id == result.id))
        slots = slots_res.scalars().all()
        assert len(slots) == 5
        weekdays = [s.weekday for s in slots]
        assert sorted(weekdays) == [0, 1, 2, 3, 4]
        for slot in slots:
            assert slot.start_time.hour == 9
            assert slot.start_time.minute == 0
            assert slot.end_time.hour == 18
            assert slot.end_time.minute == 0

