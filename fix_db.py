import asyncio
import logging
from sqlalchemy import text
from app.db.engine import engine
from app.core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("db_fix")

async def fix_database():
    """
    Manually adds missing columns to the database to resolve UndefinedColumnError.
    This is a quick fix to align the DB schema with the updated SQLModel models.
    """
    logger.info("Connecting to database to apply schema updates...")
    
    async with engine.begin() as conn:
        # 1. Add columns to 'bookings' table
        logger.info("Updating 'bookings' table...")
        await conn.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reminded_at TIMESTAMP WITH TIME ZONE;"))
        await conn.execute(text("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS followed_up_at TIMESTAMP WITH TIME ZONE;"))
        
        # 2. Add columns to 'students' table
        logger.info("Updating 'students' table...")
        await conn.execute(text("ALTER TABLE students ADD COLUMN IF NOT EXISTS notes TEXT;"))
        await conn.execute(text("ALTER TABLE students ADD COLUMN IF NOT EXISTS prepaid_balance INTEGER DEFAULT 0;"))
        await conn.execute(text("ALTER TABLE students ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;"))
        
        # 3. Create 'availability_slots' table if it doesn't exist
        logger.info("Ensuring 'availability_slots' table exists...")
        # We can use SQLModel's create_all but it might fail if other things are out of sync.
        # Here we just run a raw SQL for the specific new table if needed.
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS availability_slots (
                id SERIAL PRIMARY KEY,
                tutor_id INTEGER NOT NULL REFERENCES tutors(id),
                weekday INTEGER NOT NULL,
                start_time TIME NOT NULL,
                end_time TIME NOT NULL
            );
        """))
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_availability_slots_tutor_id ON availability_slots (tutor_id);"))

    logger.info("Database schema updated successfully!")

if __name__ == "__main__":
    asyncio.run(fix_database())
