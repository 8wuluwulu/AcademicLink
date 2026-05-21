
import asyncio
from app.db.database import engine
from sqlalchemy import text

async def check():
    async with engine.connect() as conn:
        res = await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'tutors'"))
        cols = [r[0] for r in res.all()]
        print(f"Tutors columns: {cols}")
        
        res = await conn.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'bookings'"))
        cols = [r[0] for r in res.all()]
        print(f"Bookings columns: {cols}")

if __name__ == '__main__':
    asyncio.run(check())
