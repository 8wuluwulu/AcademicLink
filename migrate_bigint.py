"""
AcademicLink — Schema Migration Script

Alters existing INTEGER columns to BIGINT for Telegram IDs,
adds the 'price' column to bookings, and adds 'telegram_username'
to the students table.

Safe to run multiple times (idempotent checks).

Run once: python migrate_bigint.py
"""

import asyncio

from sqlalchemy import text

from app.db.database import engine


async def migrate():
    async with engine.begin() as conn:
        print("Migrating tutors.tg_id to BIGINT...")
        await conn.execute(text(
            "ALTER TABLE tutors ALTER COLUMN tg_id TYPE BIGINT"
        ))

        print("Migrating students.telegram_id to BIGINT...")
        await conn.execute(text(
            "ALTER TABLE students ALTER COLUMN telegram_id TYPE BIGINT"
        ))

        print("Adding bookings.price column (if not exists)...")
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'bookings' AND column_name = 'price'
                ) THEN
                    ALTER TABLE bookings ADD COLUMN price INTEGER NOT NULL DEFAULT 0;
                END IF;
            END $$;
        """))

        print("Adding students.telegram_username column (if not exists)...")
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'students' AND column_name = 'telegram_username'
                ) THEN
                    ALTER TABLE students ADD COLUMN telegram_username VARCHAR(32) DEFAULT NULL;
                END IF;
            END $$;
        """))

    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
