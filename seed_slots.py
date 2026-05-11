import asyncio
from app.db.database import async_session_factory
from app.db.models import Tutor, AvailabilitySlot
from sqlalchemy import select, func
from datetime import time

async def seed_slots():
    async with async_session_factory() as session:
        # Get all tutors
        result = await session.execute(select(Tutor))
        tutors = result.scalars().all()
        
        for tutor in tutors:
            # Check if tutor has slots
            slot_count = await session.execute(
                select(func.count()).select_from(AvailabilitySlot).where(AvailabilitySlot.tutor_id == tutor.id)
            )
            if slot_count.scalar_one() == 0:
                print(f"Seeding slots for tutor: {tutor.name}")
                for day in range(5): # Mon-Fri
                    slot = AvailabilitySlot(
                        tutor_id=tutor.id,
                        weekday=day,
                        start_time=time(9, 0),
                        end_time=time(18, 0)
                    )
                    session.add(slot)
        
        await session.commit()
        print("Done!")

if __name__ == "__main__":
    asyncio.run(seed_slots())
