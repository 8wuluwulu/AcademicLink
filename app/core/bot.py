"""
AcademicLink — Shared Bot Instance

Holds a module-level reference to the Aiogram ``Bot`` so that any part
of the application (API routes, services, etc.) can send Telegram
messages without importing from ``main.py``.

The instance is set during the lifespan startup in ``main.py`` via
:func:`set_bot` and retrieved elsewhere via :func:`get_bot`.
"""

from __future__ import annotations

from typing import Optional

from aiogram import Bot

_bot: Optional[Bot] = None


def set_bot(bot: Bot) -> None:
    """Store the active bot instance (called once at startup)."""
    global _bot
    _bot = bot


def get_bot() -> Optional[Bot]:
    """Return the active bot instance, or ``None`` if not initialised."""
    return _bot
