"""Admin panel: /admin (password gated), batch/subject/chapter/lecture CRUD,
   /give_access, broadcast, settings, /scan, /update_channel, users."""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone

import bcrypt
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ContextTypes, ConversationHandler, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters,
)

from config import settings
from database import db
from utils import ui_helpers as ui
from jobs import channel_scanner

logger = logging.getLogger(__name__)


ADMIN_SESSIONS: set[int] = set()


def is_admin_user(tg_id: int) -> bool:
    return tg_id in settings.ADMIN_IDS


def is_admin_session(tg_id: int) -> bool:
    return tg_id in ADMIN_SESSIONS


# ─────────────────── /admin login ───────────────────
ASK_ADMIN_PW = 1000

async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_user(update.effective_user.id):
        await update.message.reply_text("🚫 Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🔐 <b>Admin Access</b>\nSend the admin password to continue:",
        parse_mode=ParseMode.HTML,
    )
    return ASK_ADMIN_PW


async def admin_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text.strip() != settings.ADMIN_PASSWORD:
        await update.message.reply_text("❌ Wrong password. Session cancelled.")
        return ConversationHandler.END
    ADMIN_SESSIONS.add(update.effective_user.id)
    await update.message.reply_text(
        "✅ <b>Admin Panel Unlocked</b>",
        reply_markup=ui.admin_menu_kb(),
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return ConversationHandler.END


# ─────────────────── admin router (button clicks) ───────────────────
async def on_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    if not is_admin_session(q.from_user.id):
        await q.edit_message_text("🔒 Session expired. Send /admin again.")
        return
    data = q.data
    if data == "adm:exit":
        ADMIN_SESSIONS.discard(q.from_user.id)
        await q.edit_message_text("🚪 Admin session closed.")
        return

    if data == "adm:batches":
        rows = db.query("SELECT * FROM batches ORDER BY created_at DESC")
        text = "📚 <b>Batches</b>\n\n"
        if not rows:
            text += "No batches yet."
        else:
            for b in rows:
                text += (f"• <b>{b['name']}</b> — <code>{b['batch_code']}</code>  ₹{b['price']}\n"
                         f"  ID: {b['batch_id']}\n")
        await q.edit_message_text(
            text + "\n\nCommands:\n"
                   "<code>/add_batch Name|Description|Price</code>\n"
                   "<code>/edit_batch batch_id|field|value</code>  (field=name/description/price)\n"
                   "<code>/del_batch batch_id</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=ui.admin_menu_kb(),
        )
        return

    if data == "adm:subjects":
        await q.edit_message_text(
            "📖 <b>Subjects</b>\n\n"
            "<code>/add_subject batch_id|Subject Name</code>\n"
            "<code>/edit_subject subject_id|New Name</code>\n"
            "<code>/del_subject subject_id</code>\n"
            "<code>/list_subjects batch_id</code>",
            parse_mode=ParseMode.HTML, reply_markup=ui.admin_menu_kb(),
        )
        return

    if data == "adm:chapters":
        await q.edit_message_text(
            "📝 <b>Chapters</b>\n\n"
            "<code>/add_chapter subject_id|Chapter Name</code>\n"
            "<code>/edit_chapter chapter_id|New Name</code>\n"
            "<code>/del_chapter chapter_id</code>\n"
            "<code>/list_chapters subject_id</code>",
            parse_mode=ParseMode.HTML, reply_markup=ui.admin_menu_kb(),
        )
        return

    if data == "adm:lectures":
        await q.edit_message_text(
            "🎥 <b>Lectures</b>\n\n"
            "<code>/add_lecture chapter_id|Name|channel_id|message_id|pdf_link|dpp_link</code>\n"
            "<code>/bulk_add chapter_id</code>  (then send lines: name|message_id|pdf|dpp — one per line, "
            "channel_id defaults to configured channel)\n"
            "<code>/edit_lecture lecture_id|field|value</code>\n"
            "<code>/del_lecture lecture_id</code>\n"
            "<code>/list_lectures chapter_id</code>",
            parse_mode=ParseMode.HTML, reply_markup=ui.admin_menu_kb(),
        )
        return

    if data == "adm:scan":
        jobs = db.query("SELECT * FROM scan_jobs ORDER BY started_at DESC LIMIT 5")
        text = "🛰️ <b>Channel Scan</b>\n\n"
        text += ("Add the bot as admin in your source channel, then run:\n"
                 "<code>/scan batch_code channel_id subject_id</code>\n"
                 "<code>/update_channel batch_code channel_id subject_id</code>\n\n"
                 "Recent jobs:\n")
        for j in jobs:
            text += (f"#{j['job_id']} • {j['status']} • {j['total_processed']}/{j['total_found']}\n")
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=ui.admin_menu_kb())
        return

    if data == "adm:users":
        row = db.query("SELECT COUNT(*) AS c FROM users", one=True)
        await q.edit_message_text(
            f"👥 <b>Users</b>\n\nTotal: <b>{row['c']}</b>\n\n"
            "<code>/list_users</code>  — first 30 users\n"
            "<code>/search_user query</code>\n"
            "<code>/user_info telegram_id</code>",
            parse_mode=ParseMode.HTML, reply_markup=ui.admin_menu_kb(),
        )
        return

    if data == "adm:broadcast":
        await q.edit_message_text(
            "📣 <b>Broadcast</b>\n\n"
            "<code>/broadcast Your message here</code>\n"
            "Also supports photos: reply to a photo with <code>/broadcast_photo caption</code>.",
            parse_mode=ParseMode.HTML, reply_markup=ui.admin_menu_kb(),
        )
        return

    if data == "adm:settings":
        rows = db.query("SELECT key, value FROM settings ORDER BY key")
        text = "⚙️ <b>Settings</b>\n\n"
        for r in rows:
            text += f"• {r['key']} = <code>{r['value']}</code>\n"
        text += ("\n<code>/set_setting key value</code>\n"
                 "<code>/set_admin_password newpassword</code>")
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=ui.admin_menu_kb())
        return

    if data == "adm:payments":
        rows = db.query(
            "SELECT p.*, b.name AS bname FROM pending_payments p "
            "JOIN batches b ON b.batch_id=p.batch_id "
            "WHERE p.status='pending' ORDER BY p.created_at DESC LIMIT 20"
        )
        text = "💰 <b>Pending Payments</b>\n\n"
        if not rows:
            text += "No pending requests."
        else:
            for r in rows:
                text += (f"• <code>{r['telegram_id']}</code> → {r['bname']} "
                         f"(req #{r['id']})\n")
        text += ("\nConfirm with:\n"
                 "<code>/give_access telegram_id batch_id username password</code>")
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=ui.admin_menu_kb())
        return


# ─────────────────── guard decorator ───────────────────
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_admin_user(uid) or not is_admin_session(uid):
            await update.message.reply_text("🔒 Send /admin first.")
            return
        return await func(update, context)
    return wrapper


# ─────────────────── batch CRUD ───────────────────
@admin_only
async def add_batch(update, context):
    raw = " ".join(context.args) if context.args else ""
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 3:
        await update.message.reply_text("Usage: /add_batch Name|Description|Price")
        return
    name, desc, price = parts[0], parts[1], parts[2]
    try:
        price = float(price)
    except ValueError:
        await update.message.reply_text("Price must be a number.")
        return
    code = "B" + secrets.token_hex(3).upper()
    row = db.execute_returning(
        "INSERT INTO batches(name, description, price, batch_code) "
        "VALUES(%s,%s,%s,%s) RETURNING batch_id, batch_code",
        (name, desc, price, code),
    )
    await update.message.reply_text(
        f"✅ Batch created\nID: <code>{row['batch_id']}</code>\n"
        f"Code: <code>{row['batch_code']}</code>",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def edit_batch(update, context):
    raw = " ".join(context.args) if context.args else ""
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3:
        await update.message.reply_text("Usage: /edit_batch batch_id|field|value  (field=name/description/price/image)")
        return
    bid, field, value = parts
    if field not in ("name", "description", "price", "image_file_id"):
        await update.message.reply_text("Bad field.")
        return
    if field == "price":
        value = float(value)
    db.execute(f"UPDATE batches SET {field}=%s, updated_at=NOW() WHERE batch_id=%s", (value, bid))
    await update.message.reply_text("✅ Updated.")


@admin_only
async def del_batch(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /del_batch batch_id"); return
    db.execute("DELETE FROM batches WHERE batch_id=%s", (context.args[0],))
    await update.message.reply_text("🗑 Deleted.")


# ─────────────────── subject / chapter CRUD ───────────────────
@admin_only
async def add_subject(update, context):
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 2:
        await update.message.reply_text("Usage: /add_subject batch_id|Subject Name"); return
    row = db.execute_returning(
        "INSERT INTO subjects(batch_id, name) VALUES(%s,%s) RETURNING subject_id",
        parts,
    )
    await update.message.reply_text(f"✅ Subject #{row['subject_id']} created.")


@admin_only
async def edit_subject(update, context):
    raw = " ".join(context.args)
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 2:
        await update.message.reply_text("Usage: /edit_subject subject_id|New Name"); return
    db.execute("UPDATE subjects SET name=%s WHERE subject_id=%s", (parts[1], parts[0]))
    await update.message.reply_text("✅ Updated.")


@admin_only
async def del_subject(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /del_subject subject_id"); return
    db.execute("DELETE FROM subjects WHERE subject_id=%s", (context.args[0],))
    await update.message.reply_text("🗑 Deleted.")


@admin_only
async def list_subjects(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /list_subjects batch_id"); return
    rows = db.query("SELECT * FROM subjects WHERE batch_id=%s", (context.args[0],))
    if not rows:
        await update.message.reply_text("No subjects."); return
    txt = "\n".join(f"#{r['subject_id']}  {r['name']}" for r in rows)
    await update.message.reply_text(txt)


@admin_only
async def add_chapter(update, context):
    raw = " ".join(context.args); parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 2:
        await update.message.reply_text("Usage: /add_chapter subject_id|Chapter Name"); return
    row = db.execute_returning(
        "INSERT INTO chapters(subject_id, name) VALUES(%s,%s) RETURNING chapter_id", parts,
    )
    await update.message.reply_text(f"✅ Chapter #{row['chapter_id']} created.")


@admin_only
async def edit_chapter(update, context):
    raw = " ".join(context.args); parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 2:
        await update.message.reply_text("Usage: /edit_chapter chapter_id|New Name"); return
    db.execute("UPDATE chapters SET name=%s WHERE chapter_id=%s", (parts[1], parts[0]))
    await update.message.reply_text("✅ Updated.")


@admin_only
async def del_chapter(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /del_chapter chapter_id"); return
    db.execute("DELETE FROM chapters WHERE chapter_id=%s", (context.args[0],))
    await update.message.reply_text("🗑 Deleted.")


@admin_only
async def list_chapters(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /list_chapters subject_id"); return
    rows = db.query("SELECT * FROM chapters WHERE subject_id=%s", (context.args[0],))
    if not rows: await update.message.reply_text("No chapters."); return
    await update.message.reply_text("\n".join(f"#{r['chapter_id']}  {r['name']}" for r in rows))


# ─────────────────── lecture CRUD ───────────────────
@admin_only
async def add_lecture(update, context):
    raw = " ".join(context.args); parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 4:
        await update.message.reply_text(
            "Usage: /add_lecture chapter_id|Name|channel_id|message_id|pdf|dpp"); return
    while len(parts) < 6: parts.append(None)
    row = db.execute_returning(
        """INSERT INTO lectures(chapter_id, name, channel_id, message_id, pdf_link, dpp_link)
           VALUES(%s,%s,%s,%s,%s,%s)
           ON CONFLICT (channel_id, message_id) DO UPDATE SET name=EXCLUDED.name
           RETURNING lecture_id""",
        (parts[0], parts[1], parts[2] or None, parts[3] or None,
         parts[4] or None, parts[5] or None),
    )
    await update.message.reply_text(f"✅ Lecture #{row['lecture_id']} saved.")


@admin_only
async def edit_lecture(update, context):
    raw = " ".join(context.args); parts = [p.strip() for p in raw.split("|")]
    if len(parts) != 3:
        await update.message.reply_text("Usage: /edit_lecture lecture_id|field|value"); return
    lid, field, value = parts
    if field not in ("name", "channel_id", "message_id", "pdf_link", "dpp_link"):
        await update.message.reply_text("Bad field."); return
    db.execute(f"UPDATE lectures SET {field}=%s WHERE lecture_id=%s", (value, lid))
    await update.message.reply_text("✅ Updated.")


@admin_only
async def del_lecture(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /del_lecture lecture_id"); return
    db.execute("DELETE FROM lectures WHERE lecture_id=%s", (context.args[0],))
    await update.message.reply_text("🗑 Deleted.")


@admin_only
async def list_lectures(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /list_lectures chapter_id"); return
    rows = db.query("SELECT * FROM lectures WHERE chapter_id=%s", (context.args[0],))
    if not rows: await update.message.reply_text("No lectures."); return
    text = "\n".join(
        f"#{r['lecture_id']}  {r['name']}  ch={r['channel_id']} msg={r['message_id']}"
        for r in rows)
    await update.message.reply_text(text[:4000])


BULK_CH_ID: dict[int, int] = {}

@admin_only
async def bulk_add(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /bulk_add chapter_id  then send lines"); return
    BULK_CH_ID[update.effective_user.id] = int(context.args[0])
    await update.message.reply_text(
        "📥 Send lecture lines (one per message OR one big message with newlines).\n"
        "Format: <code>name|message_id|pdf|dpp|channel_id(optional)</code>\n"
        "Type /done to finish.",
        parse_mode=ParseMode.HTML,
    )


@admin_only
async def bulk_done(update, context):
    BULK_CH_ID.pop(update.effective_user.id, None)
    await update.message.reply_text("✅ Bulk mode closed.")


async def bulk_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid not in BULK_CH_ID:
        return  # not in bulk mode
    if not is_admin_session(uid):
        return
    ch_id = BULK_CH_ID[uid]
    default_channel = settings.CHANNEL_IDS[0] if settings.CHANNEL_IDS else None
    added = 0
    for line in update.message.text.splitlines():
        line = line.strip()
        if not line or line.startswith("/"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        while len(parts) < 5: parts.append(None)
        name, mid, pdf, dpp, cch = parts[:5]
        db.execute(
            """INSERT INTO lectures(chapter_id,name,channel_id,message_id,pdf_link,dpp_link)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT (channel_id, message_id) DO NOTHING""",
            (ch_id, name, cch or default_channel, mid or None, pdf or None, dpp or None),
        )
        added += 1
    await update.message.reply_text(f"➕ Added {added} lectures (duplicates skipped).")


# ─────────────────── give_access ───────────────────
@admin_only
async def give_access(update, context):
    if len(context.args) < 4:
        await update.message.reply_text(
            "Usage: /give_access telegram_id batch_id username password"); return
    tg_id, bid, username, password = context.args[:4]
    row = db.query("SELECT * FROM users WHERE telegram_id=%s", (tg_id,), one=True)
    if not row:
        await update.message.reply_text("❌ User hasn't /start-ed the bot yet."); return
    b = db.query("SELECT * FROM batches WHERE batch_id=%s", (bid,), one=True)
    if not b:
        await update.message.reply_text("❌ Batch not found."); return
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    current = row.get("purchased_batches") or []
    if int(bid) not in current:
        current = list(current) + [int(bid)]
    db.execute(
        """UPDATE users
           SET edu_username=%s, edu_password=%s, purchased_batches=%s
           WHERE telegram_id=%s""",
        (username, hashed, current, tg_id),
    )
    db.execute(
        "UPDATE pending_payments SET status='confirmed' "
        "WHERE telegram_id=%s AND batch_id=%s AND status='pending'",
        (tg_id, bid),
    )
    try:
        await context.bot.send_message(
            int(tg_id),
            f"🎉 <b>Access Granted!</b>\n\n"
            f"📚 Batch: <b>{b['name']}</b>\n\n"
            f"🔐 <b>Your Credentials</b>\n"
            f"Username: <code>{username}</code>\n"
            f"Password: <code>{password}</code>\n\n"
            f"⚠️ Keep these safe. You now have <b>{settings.PAID_OPEN_LIMIT}</b> lecture/PDF opens for this batch.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"Access granted but couldn't DM user: {e}"); return
    await update.message.reply_text("✅ Access granted and credentials sent.")


# ─────────────────── users ───────────────────
@admin_only
async def list_users(update, context):
    rows = db.query(
        "SELECT telegram_id, full_name, telegram_username, joined_at, "
        "cardinality(purchased_batches) AS pb FROM users ORDER BY joined_at DESC LIMIT 30"
    )
    text = "👥 <b>Users (latest 30)</b>\n\n"
    for r in rows:
        text += (f"• {r['full_name'] or ''}  @{r['telegram_username'] or '-'}  "
                 f"<code>{r['telegram_id']}</code>  batches:{r['pb']}\n")
    await update.message.reply_text(text[:4000], parse_mode=ParseMode.HTML)


@admin_only
async def search_user(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /search_user query"); return
    q = "%" + " ".join(context.args) + "%"
    rows = db.query(
        "SELECT telegram_id, full_name, telegram_username FROM users "
        "WHERE full_name ILIKE %s OR telegram_username ILIKE %s OR CAST(telegram_id AS TEXT) ILIKE %s "
        "LIMIT 30",
        (q, q, q),
    )
    if not rows: await update.message.reply_text("No match."); return
    text = "\n".join(
        f"{r['full_name'] or ''} @{r['telegram_username'] or '-'} <code>{r['telegram_id']}</code>"
        for r in rows)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@admin_only
async def user_info(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /user_info telegram_id"); return
    r = db.query("SELECT * FROM users WHERE telegram_id=%s", (context.args[0],), one=True)
    if not r: await update.message.reply_text("Not found."); return
    refs = db.query("SELECT COUNT(*) c FROM referrals WHERE referrer_id=%s", (r["telegram_id"],), one=True)["c"]
    await update.message.reply_text(
        f"👤 <b>{r['full_name'] or ''}</b>\n"
        f"ID: <code>{r['telegram_id']}</code>\n"
        f"@{r['telegram_username'] or '-'}\n"
        f"Joined: {r['joined_at']}\n"
        f"Trial: {r['trial_active']} ({r['trial_open_count']}/{settings.FREE_TRIAL_OPEN_LIMIT})\n"
        f"Paid batches: {r['purchased_batches']}\n"
        f"Referral code: <code>{r['referral_code']}</code>\n"
        f"Bonus hours: {r['referral_bonus_hours']}\n"
        f"Referrals: {refs}",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────── broadcast ───────────────────
@admin_only
async def broadcast(update, context):
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast Your message"); return
    ids = [r["telegram_id"] for r in db.query("SELECT telegram_id FROM users")]
    sent = failed = 0
    for tid in ids:
        try:
            await context.bot.send_message(tid, text, parse_mode=ParseMode.HTML)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.03)
    await update.message.reply_text(f"📣 Sent: {sent} • Failed: {failed}")


# ─────────────────── settings ───────────────────
@admin_only
async def set_setting(update, context):
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /set_setting key value"); return
    key = context.args[0]; value = " ".join(context.args[1:])
    db.execute(
        "INSERT INTO settings(key,value) VALUES(%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (key, value),
    )
    # live-reload common keys
    m = {
        "FREE_TRIAL_HOURS": "FREE_TRIAL_HOURS",
        "FREE_TRIAL_OPEN_LIMIT": "FREE_TRIAL_OPEN_LIMIT",
        "PAID_OPEN_LIMIT": "PAID_OPEN_LIMIT",
        "LECTURE_DELETE_AFTER_HOURS": "LECTURE_DELETE_AFTER_HOURS",
        "SLIDING_WINDOW_SIZE": "SLIDING_WINDOW_SIZE",
        "REFER_BONUS_HOURS": "REFER_BONUS_HOURS",
    }
    if key in m:
        try:
            setattr(settings, m[key], int(value))
        except ValueError:
            pass
    await update.message.reply_text("✅ Setting updated.")


@admin_only
async def set_admin_password(update, context):
    if not context.args:
        await update.message.reply_text("Usage: /set_admin_password newpw"); return
    settings.ADMIN_PASSWORD = " ".join(context.args)
    db.execute(
        "INSERT INTO settings(key,value) VALUES('ADMIN_PASSWORD',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        (settings.ADMIN_PASSWORD,),
    )
    await update.message.reply_text("✅ Admin password updated (in-memory + DB).")


# ─────────────────── scanner triggers ───────────────────
@admin_only
async def scan_cmd(update, context):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /scan batch_code channel_id subject_id\n"
            "channel_id must be numeric (e.g. -1001234567890)"); return
    code, ch_id, sub_id = context.args[0], int(context.args[1]), int(context.args[2])
    b = db.query("SELECT * FROM batches WHERE batch_code=%s", (code,), one=True)
    if not b: await update.message.reply_text("Batch code not found."); return
    await update.message.reply_text(f"🛰️ Scan started for {b['name']}. You'll be notified on progress…")
    asyncio.create_task(
        channel_scanner.run_scan(context.bot, update.effective_user.id,
                                 b["batch_id"], sub_id, ch_id, resume=False)
    )


@admin_only
async def update_channel_cmd(update, context):
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /update_channel batch_code channel_id subject_id"); return
    code, ch_id, sub_id = context.args[0], int(context.args[1]), int(context.args[2])
    b = db.query("SELECT * FROM batches WHERE batch_code=%s", (code,), one=True)
    if not b: await update.message.reply_text("Batch code not found."); return
    await update.message.reply_text(f"🔄 Incremental scan started for {b['name']}…")
    asyncio.create_task(
        channel_scanner.run_scan(context.bot, update.effective_user.id,
                                 b["batch_id"], sub_id, ch_id, resume=True)
    )


# ─────────────────── conversation builder ───────────────────
def build_admin_conv():
    return ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
        states={ASK_ADMIN_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_password)]},
        fallbacks=[CommandHandler("cancel", admin_cancel)],
        conversation_timeout=120,
    )
