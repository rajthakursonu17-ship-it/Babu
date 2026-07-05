"""Shriji Institute Telegram Education Bot — entry point."""
from __future__ import annotations

import logging
import sys
from datetime import time as dtime, timezone

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
    MessageHandler, filters,
)

from config import settings
from database import db
from handlers import user_handlers as uh
from handlers import admin_handlers as ah
from jobs import auto_delete

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger("shriji-bot")


async def _bootstrap_settings() -> None:
    """Hydrate in-memory settings from DB (if admin previously changed them)."""
    rows = db.query("SELECT key, value FROM settings")
    mapping = {
        "FREE_TRIAL_HOURS": "FREE_TRIAL_HOURS",
        "FREE_TRIAL_OPEN_LIMIT": "FREE_TRIAL_OPEN_LIMIT",
        "PAID_OPEN_LIMIT": "PAID_OPEN_LIMIT",
        "LECTURE_DELETE_AFTER_HOURS": "LECTURE_DELETE_AFTER_HOURS",
        "SLIDING_WINDOW_SIZE": "SLIDING_WINDOW_SIZE",
        "REFER_BONUS_HOURS": "REFER_BONUS_HOURS",
    }
    for r in rows:
        k = r["key"]
        if k in mapping:
            try:
                setattr(settings, mapping[k], int(r["value"]))
            except ValueError:
                pass
        elif k == "ADMIN_PASSWORD":
            settings.ADMIN_PASSWORD = r["value"]


async def sweeper_job(context) -> None:
    try:
        await auto_delete.sweep_expired(context.bot)
    except Exception as e:
        logger.exception("sweeper error: %s", e)


def build_app() -> Application:
    app = ApplicationBuilder().token(settings.BOT_TOKEN).build()

    # user handlers
    app.add_handler(CommandHandler("start", uh.start))
    app.add_handler(CommandHandler("refer", uh.refer_cmd))
    app.add_handler(CommandHandler("myaccount", uh.myaccount_cmd))
    app.add_handler(CommandHandler("support", uh.support_cmd))

    # admin login conversation
    app.add_handler(ah.build_admin_conv())

    # admin commands
    for cmd, fn in {
        "add_batch": ah.add_batch, "edit_batch": ah.edit_batch, "del_batch": ah.del_batch,
        "add_subject": ah.add_subject, "edit_subject": ah.edit_subject,
        "del_subject": ah.del_subject, "list_subjects": ah.list_subjects,
        "add_chapter": ah.add_chapter, "edit_chapter": ah.edit_chapter,
        "del_chapter": ah.del_chapter, "list_chapters": ah.list_chapters,
        "add_lecture": ah.add_lecture, "edit_lecture": ah.edit_lecture,
        "del_lecture": ah.del_lecture, "list_lectures": ah.list_lectures,
        "bulk_add": ah.bulk_add, "done": ah.bulk_done,
        "give_access": ah.give_access, "list_users": ah.list_users,
        "search_user": ah.search_user, "user_info": ah.user_info,
        "broadcast": ah.broadcast, "set_setting": ah.set_setting,
        "set_admin_password": ah.set_admin_password,
        "scan": ah.scan_cmd, "update_channel": ah.update_channel_cmd,
    }.items():
        app.add_handler(CommandHandler(cmd, fn))

    # bulk paste (admin only, non-command text while in bulk mode)
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(list(settings.ADMIN_IDS)),
        ah.bulk_text_handler,
    ))

    # callback queries (admin buttons before user buttons)
    app.add_handler(CallbackQueryHandler(ah.on_admin_cb, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(uh.on_callback))

    # scheduled sweeper every 5 min
    app.job_queue.run_repeating(sweeper_job, interval=300, first=30, name="sweeper")

    async def post_init(a: Application) -> None:
        await _bootstrap_settings()
        me = await a.bot.get_me()
        logger.info("Bot online as @%s (id=%s)", me.username, me.id)

    app.post_init = post_init
    return app


def main() -> None:
    logger.info("Initialising DB pool…")
    db.init_pool()
    logger.info("Applying schema (idempotent)…")
    db.apply_schema()

    app = build_app()
    logger.info("Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
