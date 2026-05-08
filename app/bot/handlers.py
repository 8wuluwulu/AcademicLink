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
    """Greet the user on /start and display their Telegram ID."""
    tg_id = message.from_user.id
    await message.answer(
        f"Your ID: {tg_id}\n\n"
        "👋 Welcome to <b>AcademicLink</b>!\n"
        "I'll help you find and book a tutor.\n\n"
        f"🆔 Your Telegram ID is: <code>{tg_id}</code>\n"
        "Add this ID to your <code>.env</code> as "
        "<code>DEFAULT_TUTOR_TG_ID</code>.\n\n"
        "Use /help to see available commands.",
        parse_mode="HTML",
    )

