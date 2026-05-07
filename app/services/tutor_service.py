"""
AcademicLink — Tutor Service

Startup helper to ensure at least one tutor exists in the database.
Designed to be called during application initialisation.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Tutor

logger = logging.getLogger(__name__)


async def ensure_tutor_exists(session: AsyncSession) -> Tutor:
    """
    Guarantee that the database contains at least one tutor.

    If the ``tutors`` table is empty, a *Default Tutor* is created
    using ``DEFAULT_TUTOR_TG_ID`` and ``DEFAULT_TUTOR_NAME`` from
    the application settings.

    Returns
    -------
    Tutor
        The existing or newly created tutor record.

    Raises
    ------
    RuntimeError
        If ``DEFAULT_TUTOR_TG_ID`` is not configured and no tutor
        exists yet.
    """
    # Check whether *any* tutor row exists
    count_stmt = select(func.count()).select_from(Tutor)
    result = await session.execute(count_stmt)
    tutor_count = result.scalar_one()

    if tutor_count > 0:
        # Return the first active tutor (or any tutor as fallback)
        stmt = (
            select(Tutor)
            .where(Tutor.is_active.is_(True))
            .limit(1)
        )
        result = await session.execute(stmt)
        tutor = result.scalar_one_or_none()

        if tutor is None:
            # All tutors are inactive — return the first one anyway
            stmt = select(Tutor).limit(1)
            result = await session.execute(stmt)
            tutor = result.scalar_one()

        logger.info("Tutor already exists: %s (tg_id=%d)", tutor.name, tutor.tg_id)
        return tutor

    # ── No tutors — seed a default one ───────────────────────────────
    if settings.default_tutor_tg_id is None:
        raise RuntimeError(
            "No tutors in the database and DEFAULT_TUTOR_TG_ID is not set. "
            "Add DEFAULT_TUTOR_TG_ID to your .env file."
        )

    tutor = Tutor(
        tg_id=settings.default_tutor_tg_id,
        name=settings.default_tutor_name,
        is_active=True,
    )
    session.add(tutor)
    await session.commit()
    await session.refresh(tutor)

    logger.info(
        "Default tutor created: %s (tg_id=%d)",
        tutor.name,
        tutor.tg_id,
    )
    return tutor
