import asyncio
import sys
import os

# Ensure the project root is in the path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import delete
from app.db.database import async_session_factory
from app.db.models import Tutor, Booking, Service, AvailabilitySlot, TutorAbsence, StudentTutorLink, Student

async def main():
    print("Deleting ALL tutors and related records from the database...")

    async with async_session_factory() as session:
        # Delete bookings
        res_bookings = await session.execute(delete(Booking))
        print(f"Deleted bookings: {res_bookings.rowcount}")

        # Delete services
        res_services = await session.execute(delete(Service))
        print(f"Deleted services: {res_services.rowcount}")

        # Delete availability slots
        res_slots = await session.execute(delete(AvailabilitySlot))
        print(f"Deleted availability slots: {res_slots.rowcount}")

        # Delete absences
        res_absences = await session.execute(delete(TutorAbsence))
        print(f"Deleted absences: {res_absences.rowcount}")

        # Delete student tutor links
        res_links = await session.execute(delete(StudentTutorLink))
        print(f"Deleted student-tutor links: {res_links.rowcount}")

        # Delete tutors
        res_tutors = await session.execute(delete(Tutor))
        print(f"Deleted tutors: {res_tutors.rowcount}")

        # Delete all students
        res_students = await session.execute(delete(Student))
        print(f"Deleted students: {res_students.rowcount}")

        await session.commit()
        print("All tutors and related data deleted successfully!")

if __name__ == "__main__":
    asyncio.run(main())
