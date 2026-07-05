"""Background job that deletes videos 15h after they were sent to the user."""
from __future__ import annotations

import logging

from telegram import Bot

from database import db

logger = logging.getLogger(__name__)


async def sweep_expired(bot: Bot) -> None:
    rows = db.query(
        """SELECT id, telegram_id, sent_message_id
           FROM user_lecture_access
           WHERE deleted=FALSE AND delete_at <= NOW()
           LIMIT 500"""
    )
    if not rows:
        return
    for r in rows:
        try:
            await bot.delete_message(r["telegram_id"], r["sent_message_id"])
        except Exception as e:
            logger.debug("sweep delete failed: %s", e)
        db.execute("UPDATE user_lecture_access SET deleted=TRUE WHERE id=%s", (r["id"],))
    logger.info("sweep_expired: purged %d messages", len(rows))
