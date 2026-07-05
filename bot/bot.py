"""Shriji Institute Telegram Education Bot — entry point (button-driven)."""
from __future__ import annotations

import logging
import sys

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
    rows = db.query("SELECT key, value FROM settings")
    keys = {"FREE_TRIAL_HOURS", "FREE_TRIAL_OPEN_LIMIT", "PAID_OPEN_LIMIT",
            "LECTURE_DELETE_AFTER_HOURS", "SLIDING_WINDOW_SIZE", "REFER_BONUS_HOURS"}
    for r in rows:
        k = r["key"]
        if k in keys:
            try:
                setattr(settings, k, int(r["value"]))
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

    # ── USER COMMANDS (only these are commands) ─────────────
    app.add_handler(CommandHandler("start", uh.start))
    app.add_handler(CommandHandler("refer", uh.refer_cmd))
    app.add_handler(CommandHandler("myaccount", uh.myaccount_cmd))
    app.add_handler(CommandHandler("support", uh.support_cmd))

    # ── ADMIN ENTRY ─────────────
    app.add_handler(ah.build_admin_conv())          # /admin
    app.add_handler(CommandHandler("give_access", ah.give_access_cmd))  # optional shortcut

    # ── CALLBACK QUERIES ─────────────
    app.add_handler(CallbackQueryHandler(ah.on_admin_cb, pattern=r"^adm:"))
    app.add_handler(CallbackQueryHandler(uh.on_callback))

    # ── ADMIN FREE-FORM INPUT (only for admin users, non-command) ─────
    admin_only_filter = filters.User(list(settings.ADMIN_IDS))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & admin_only_filter,
        ah.admin_text_router,
    ))
    app.add_handler(MessageHandler(
        filters.PHOTO & admin_only_filter,
        ah.admin_photo_router,
    ))

    # ── SWEEPER ─────────────
    app.job_queue.run_repeating(sweeper_job, interval=300, first=30, name="sweeper")

    async def post_init(a: Application) -> None:
        await _bootstrap_settings()
        # Publish a minimal command list — everything else is buttons
        from telegram import BotCommand
        await a.bot.set_my_commands([
            BotCommand("start", "Start the bot / restart"),
            BotCommand("refer", "Get your referral link"),
            BotCommand("myaccount", "Your access & stats"),
            BotCommand("support", "Contact admin"),
            BotCommand("admin", "Admin panel (password)"),
            BotCommand("cancel", "Cancel current action"),
        ])
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
