"""
AcademicLink — Database Engine (backward-compatibility shim)

All functionality has moved to ``app.db.database``.
This module re-exports the public API so existing imports keep working.
"""

from app.db.database import (  # noqa: F401
    async_session_factory,
    engine,
    get_session,
    init_db,
)
