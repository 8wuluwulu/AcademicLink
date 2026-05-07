"""
AcademicLink — Bot Handlers

Register all Aiogram routers / handlers here.
"""

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

router = Router(name="main_router")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    """Greet the user on /start."""
    await message.answer(
        "👋 Welcome to <b>AcademicLink</b>!\n"
        "I'll help you find and book a tutor.\n\n"
        "Use /help to see available commands.",
        parse_mode="HTML",
    )
