"""User-facing handlers: /start, browse, refer, buy, my access."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from telegram import Update, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from config import settings
from database import db
from utils import ui_helpers as ui

logger = logging.getLogger(__name__)


# ─────────────────────────── helpers ───────────────────────────
def _make_ref_code(tg_id: int) -> str:
    return f"SHRIJI{tg_id % 100000}{secrets.token_hex(2).upper()}"


def _get_or_create_user(update: Update, referred_by: int | None = None) -> dict:
    u = update.effective_user
    row = db.query("SELECT * FROM users WHERE telegram_id=%s", (u.id,), one=True)
    if row:
        # keep name/username fresh
        db.execute(
            "UPDATE users SET full_name=%s, telegram_username=%s WHERE telegram_id=%s",
            (u.full_name, u.username, u.id),
        )
        return row
    ref_code = _make_ref_code(u.id)
    now = datetime.now(timezone.utc)
    db.execute(
        """INSERT INTO users
           (telegram_id, full_name, telegram_username, trial_start, trial_active,
            referral_code, referred_by, joined_at)
           VALUES (%s,%s,%s,%s,TRUE,%s,%s,%s)""",
        (u.id, u.full_name, u.username, now, ref_code, referred_by, now),
    )
    return db.query("SELECT * FROM users WHERE telegram_id=%s", (u.id,), one=True)


def _apply_referral_bonus(referrer_id: int, referred_id: int) -> None:
    existing = db.query(
        "SELECT 1 FROM referrals WHERE referred_id=%s", (referred_id,), one=True
    )
    if existing:
        return
    db.execute(
        "INSERT INTO referrals(referrer_id, referred_id, bonus_applied) "
        "VALUES(%s,%s,TRUE)",
        (referrer_id, referred_id),
    )
    db.execute(
        "UPDATE users SET referral_bonus_hours = referral_bonus_hours + %s "
        "WHERE telegram_id=%s",
        (settings.REFER_BONUS_HOURS, referrer_id),
    )


def _trial_status(user: dict) -> tuple[bool, str]:
    """Returns (still_active, reason)."""
    if not user.get("trial_active"):
        return False, "trial_ended"
    if user.get("trial_open_count", 0) >= settings.FREE_TRIAL_OPEN_LIMIT:
        return False, "opens_exhausted"
    ts = user.get("trial_start")
    if ts is None:
        return False, "trial_ended"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    bonus = int(user.get("referral_bonus_hours", 0) or 0)
    ends = ts + timedelta(hours=settings.FREE_TRIAL_HOURS + bonus)
    if datetime.now(timezone.utc) > ends:
        return False, "time_up"
    return True, "ok"


def _has_batch_access(user: dict, batch_id: int) -> tuple[bool, str]:
    """Paid access or active trial?"""
    if batch_id in (user.get("purchased_batches") or []):
        counts = user.get("paid_open_count") or {}
        used = int(counts.get(str(batch_id), 0))
        if used >= settings.PAID_OPEN_LIMIT:
            return False, "paid_limit"
        return True, "paid"
    alive, _ = _trial_status(user)
    if alive:
        return True, "trial"
    return False, "no_access"


# ─────────────────────────── /start ───────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ref_by: int | None = None
    if context.args:
        code = context.args[0].strip()
        row = db.query(
            "SELECT telegram_id FROM users WHERE referral_code=%s", (code,), one=True
        )
        if row and row["telegram_id"] != update.effective_user.id:
            ref_by = row["telegram_id"]

    existed = db.query(
        "SELECT 1 FROM users WHERE telegram_id=%s",
        (update.effective_user.id,),
        one=True,
    )
    user = _get_or_create_user(update, referred_by=ref_by)
    if not existed and ref_by:
        _apply_referral_bonus(ref_by, update.effective_user.id)

    await update.message.reply_text(
        ui.welcome_text(
            update.effective_user.full_name,
            settings.FREE_TRIAL_HOURS,
            settings.FREE_TRIAL_OPEN_LIMIT,
        ),
        reply_markup=ui.main_menu_kb(),
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────── menu router ───────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    user = _get_or_create_user(update)

    if data == "home":
        await q.edit_message_text(
            "🏠 <b>Main Menu</b>\nWhat would you like to do?",
            reply_markup=ui.main_menu_kb(),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "batches" or data == "buy_menu":
        rows = db.query("SELECT * FROM batches ORDER BY created_at DESC")
        if not rows:
            await q.edit_message_text(
                "📚 No batches available yet. Please check back later!",
                reply_markup=ui.main_menu_kb(),
            )
            return
        await q.edit_message_text(
            "📚 <b>Available Batches</b>\nSelect one to view details:",
            reply_markup=ui.batches_kb(rows),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("batch:"):
        bid = int(data.split(":")[1])
        b = db.query("SELECT * FROM batches WHERE batch_id=%s", (bid,), one=True)
        if not b:
            await q.edit_message_text("Batch not found.", reply_markup=ui.main_menu_kb())
            return
        purchased = bid in (user.get("purchased_batches") or [])
        text = (
            f"📚 <b>{b['name']}</b>\n"
            f"🏷 Code: <code>{b['batch_code']}</code>\n"
            f"💰 Price: ₹{b['price']}\n\n"
            f"{b.get('description') or 'A curated learning journey by Shriji Institute.'}\n\n"
            f"{'✅ You own this batch' if purchased else '🔒 Locked — buy to unlock full access'}"
        )
        if b.get("image_file_id"):
            try:
                await q.message.delete()
                await context.bot.send_photo(
                    q.message.chat_id,
                    photo=b["image_file_id"],
                    caption=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=ui.batch_detail_kb(bid, purchased),
                )
                return
            except Exception:
                pass
        await q.edit_message_text(
            text,
            reply_markup=ui.batch_detail_kb(bid, purchased),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("subjects:"):
        bid = int(data.split(":")[1])
        subs = db.query(
            "SELECT * FROM subjects WHERE batch_id=%s ORDER BY subject_id", (bid,)
        )
        if not subs:
            await q.edit_message_text(
                "📖 No subjects added yet in this batch.",
                reply_markup=ui.batch_detail_kb(bid, bid in (user.get("purchased_batches") or [])),
            )
            return
        await _safe_edit(q,
            f"📖 <b>Subjects</b>\nTap a subject to view chapters:",
            reply_markup=ui.subjects_kb(bid, subs),
        )
        return

    if data.startswith("subject:"):
        sid = int(data.split(":")[1])
        sub = db.query("SELECT * FROM subjects WHERE subject_id=%s", (sid,), one=True)
        if not sub:
            return
        chaps = db.query(
            "SELECT * FROM chapters WHERE subject_id=%s ORDER BY chapter_id", (sid,)
        )
        if not chaps:
            await _safe_edit(q,
                "📝 No chapters added yet.",
                reply_markup=ui.subjects_kb(sub["batch_id"],
                    db.query("SELECT * FROM subjects WHERE batch_id=%s", (sub["batch_id"],))),
            )
            return
        await _safe_edit(q,
            f"📝 <b>{sub['name']} — Chapters</b>",
            reply_markup=ui.chapters_kb(sid, sub["batch_id"], chaps),
        )
        return

    if data.startswith("chapter:"):
        cid = int(data.split(":")[1])
        ch = db.query("SELECT * FROM chapters WHERE chapter_id=%s", (cid,), one=True)
        if not ch:
            return
        lecs = db.query(
            "SELECT * FROM lectures WHERE chapter_id=%s ORDER BY lecture_id", (cid,)
        )
        if not lecs:
            await _safe_edit(q, "🎥 No lectures yet in this chapter.",
                reply_markup=ui.chapters_kb(ch["subject_id"], 0,
                    db.query("SELECT * FROM chapters WHERE subject_id=%s", (ch["subject_id"],))))
            return
        await _safe_edit(q,
            f"🎥 <b>{ch['name']} — Lectures</b>",
            reply_markup=ui.lectures_kb(cid, ch["subject_id"], lecs),
        )
        return

    if data.startswith("lecture:"):
        lid = int(data.split(":")[1])
        lec = db.query("SELECT * FROM lectures WHERE lecture_id=%s", (lid,), one=True)
        if not lec:
            return
        await _safe_edit(q,
            f"🎬 <b>{lec['name']}</b>\n\nChoose what you'd like to open:",
            reply_markup=ui.lecture_actions_kb(lec, lec["chapter_id"]),
        )
        return

    if data.startswith(("watch:", "pdf:", "dpp:")):
        await _deliver_content(update, context, user, data)
        return

    if data.startswith("buy:"):
        bid = int(data.split(":")[1])
        await _handle_buy(update, context, user, bid)
        return

    if data == "refer":
        await _handle_refer(update, context, user)
        return

    if data == "profile":
        await _handle_profile(update, context, user)
        return

    if data == "myaccess":
        await _handle_my_access(update, context, user)
        return

    if data == "help":
        await _safe_edit(q,
            "ℹ️ <b>Help</b>\n\n"
            "• /start — restart bot\n"
            "• /refer — your referral link\n"
            "• /myaccount — your access & stats\n"
            "• /support — talk to admin\n\n"
            "Videos auto-delete after 15h. Notes/DPP stay with you.",
            reply_markup=ui.main_menu_kb(),
        )
        return


async def _safe_edit(q, text, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        try:
            await q.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ───────────────────────── content delivery ─────────────────────────
async def _deliver_content(update, context, user, data):
    q = update.callback_query
    kind, lid = data.split(":")
    lid = int(lid)
    lec = db.query("SELECT * FROM lectures WHERE lecture_id=%s", (lid,), one=True)
    if not lec:
        await q.answer("Lecture missing", show_alert=True)
        return

    # figure out batch this lecture belongs to
    row = db.query(
        """SELECT s.batch_id AS bid FROM chapters c
           JOIN subjects s ON s.subject_id = c.subject_id
           WHERE c.chapter_id=%s""",
        (lec["chapter_id"],), one=True,
    )
    batch_id = row["bid"] if row else None

    ok, mode = _has_batch_access(user, batch_id)
    if not ok:
        text = ("🔒 <b>Access limit reached</b>\n\n"
                "Your free trial has ended or opens are exhausted.\n"
                "💳 Purchase this batch to unlock <b>100 opens</b>.")
        kb = ui.kb([[("💳 Buy Now", f"buy:{batch_id}")],
                    [("🏠 Home", "home")]])
        await q.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    if kind == "pdf":
        link = lec.get("pdf_link")
        if not link:
            await q.answer("PDF not available", show_alert=True); return
        await q.message.reply_text(
            f"📄 <b>{lec['name']} — Notes</b>\n{link}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        _bump_open_counter(user, mode, batch_id)
        return

    if kind == "dpp":
        link = lec.get("dpp_link")
        if not link:
            await q.answer("DPP not available", show_alert=True); return
        await q.message.reply_text(
            f"🧪 <b>{lec['name']} — DPP</b>\n{link}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=False,
        )
        _bump_open_counter(user, mode, batch_id)
        return

    # kind == watch  → forward video with protection
    if not lec.get("message_id") or not lec.get("channel_id"):
        await q.answer("Video not linked yet", show_alert=True); return
    try:
        sent = await context.bot.copy_message(
            chat_id=q.message.chat_id,
            from_chat_id=lec["channel_id"],
            message_id=lec["message_id"],
            protect_content=True,
        )
    except Exception as e:
        logger.exception("copy_message failed")
        await q.message.reply_text(f"⚠️ Couldn't fetch the video ({e}).")
        return

    # record access
    delete_at = datetime.now(timezone.utc) + timedelta(hours=settings.LECTURE_DELETE_AFTER_HOURS)
    seq_row = db.query(
        "SELECT COALESCE(MAX(sequence_number),0)+1 AS n FROM user_lecture_access "
        "WHERE telegram_id=%s AND batch_id=%s AND deleted=FALSE",
        (user["telegram_id"], batch_id), one=True,
    )
    seq = seq_row["n"]
    db.execute(
        """INSERT INTO user_lecture_access
           (telegram_id, lecture_id, batch_id, sent_message_id,
            accessed_at, delete_at, sequence_number)
           VALUES(%s,%s,%s,%s,NOW(),%s,%s)""",
        (user["telegram_id"], lid, batch_id, sent.message_id, delete_at, seq),
    )
    _bump_open_counter(user, mode, batch_id)

    # sliding window cleanup
    old = db.query(
        """SELECT id, sent_message_id FROM user_lecture_access
           WHERE telegram_id=%s AND batch_id=%s AND deleted=FALSE
           ORDER BY sequence_number ASC""",
        (user["telegram_id"], batch_id),
    )
    if len(old) > settings.SLIDING_WINDOW_SIZE:
        to_remove = old[: len(old) - settings.SLIDING_WINDOW_SIZE]
        for r in to_remove:
            try:
                await context.bot.delete_message(q.message.chat_id, r["sent_message_id"])
            except Exception:
                pass
            db.execute("UPDATE user_lecture_access SET deleted=TRUE WHERE id=%s", (r["id"],))

    await q.message.reply_text(
        f"✅ Video delivered! It will auto-delete in "
        f"<b>{settings.LECTURE_DELETE_AFTER_HOURS}h</b>.",
        parse_mode=ParseMode.HTML,
    )


def _bump_open_counter(user: dict, mode: str, batch_id: int | None) -> None:
    if mode == "trial":
        db.execute(
            "UPDATE users SET trial_open_count = trial_open_count + 1 "
            "WHERE telegram_id=%s",
            (user["telegram_id"],),
        )
    elif mode == "paid" and batch_id is not None:
        db.execute(
            """UPDATE users
               SET paid_open_count = jsonb_set(
                   COALESCE(paid_open_count,'{}'::jsonb),
                   ARRAY[%s::text],
                   to_jsonb(COALESCE((paid_open_count->>%s)::int,0)+1)
               ) WHERE telegram_id=%s""",
            (str(batch_id), str(batch_id), user["telegram_id"]),
        )


# ─────────────────────────── /buy flow ───────────────────────────
async def _handle_buy(update, context, user, batch_id: int):
    q = update.callback_query
    b = db.query("SELECT * FROM batches WHERE batch_id=%s", (batch_id,), one=True)
    if not b:
        return
    db.execute(
        "INSERT INTO pending_payments(telegram_id, batch_id) VALUES(%s,%s)",
        (user["telegram_id"], batch_id),
    )
    # notify admins
    for admin_id in settings.ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"💰 <b>New Purchase Request</b>\n\n"
                f"👤 {user.get('full_name') or ''} (@{user.get('telegram_username') or '-'})\n"
                f"🆔 <code>{user['telegram_id']}</code>\n"
                f"📚 Batch: <b>{b['name']}</b> (<code>{b['batch_code']}</code>)\n"
                f"💵 Price: ₹{b['price']}\n\n"
                f"After confirming payment, run:\n"
                f"<code>/give_access {user['telegram_id']} {b['batch_id']} USERNAME PASSWORD</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    await q.message.reply_text(
        f"💳 <b>Purchase Request Sent</b>\n\n"
        f"📚 Batch: <b>{b['name']}</b>\n"
        f"💵 Price: ₹{b['price']}\n\n"
        f"👉 Please pay via UPI / Bank and share the screenshot with admin.\n"
        f"Once verified, your credentials will be delivered here automatically.",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────── refer ───────────────────────────
async def _handle_refer(update, context, user):
    q = update.callback_query
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={user['referral_code']}"
    total = db.query(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s",
        (user["telegram_id"],), one=True,
    )["c"]
    await _safe_edit(q,
        f"🔗 <b>Your Referral Program</b>\n\n"
        f"Share your link and earn <b>+{settings.REFER_BONUS_HOURS} hours</b> "
        f"of extra access for every friend who joins!\n\n"
        f"🎟 Your code: <code>{user['referral_code']}</code>\n"
        f"🔗 Link: {link}\n\n"
        f"👥 Total referrals: <b>{total}</b>\n"
        f"⏳ Bonus hours earned: <b>{user.get('referral_bonus_hours',0)}</b>",
        reply_markup=ui.kb([[("🏠 Home", "home")]]),
    )


async def refer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_or_create_user(update)
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start={user['referral_code']}"
    total = db.query(
        "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=%s",
        (user["telegram_id"],), one=True,
    )["c"]
    await update.message.reply_text(
        f"🔗 <b>Your Referral Program</b>\n\n"
        f"🎟 Code: <code>{user['referral_code']}</code>\n"
        f"🔗 Link: {link}\n"
        f"👥 Referrals: <b>{total}</b>\n"
        f"⏳ Bonus hours: <b>{user.get('referral_bonus_hours',0)}</b>",
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────── profile / my access ───────────────────────────
async def _handle_profile(update, context, user):
    q = update.callback_query
    alive, _ = _trial_status(user)
    purchased = user.get("purchased_batches") or []
    text = (
        f"👤 <b>Your Profile</b>\n\n"
        f"Name: <b>{user.get('full_name') or ''}</b>\n"
        f"Username: @{user.get('telegram_username') or '-'}\n"
        f"Telegram ID: <code>{user['telegram_id']}</code>\n"
        f"Joined: {user['joined_at'].strftime('%d %b %Y')}\n\n"
        f"🎁 Trial active: {'✅' if alive else '❌'}\n"
        f"Trial opens used: {user.get('trial_open_count',0)}/{settings.FREE_TRIAL_OPEN_LIMIT}\n"
        f"⭐ Purchased batches: <b>{len(purchased)}</b>\n"
        f"⏳ Bonus hours: {user.get('referral_bonus_hours',0)}"
    )
    if user.get("edu_username"):
        text += (
            f"\n\n🔐 <b>Login Credentials</b>\n"
            f"Username: <code>{user['edu_username']}</code>\n"
            f"(Password shared privately after purchase)"
        )
    await _safe_edit(q, text, reply_markup=ui.main_menu_kb())


async def myaccount_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = _get_or_create_user(update)
    alive, _ = _trial_status(user)
    purchased = user.get("purchased_batches") or []
    await update.message.reply_text(
        f"👤 <b>Your Account</b>\n\n"
        f"Trial active: {'✅' if alive else '❌'}\n"
        f"Opens used: {user.get('trial_open_count',0)}/{settings.FREE_TRIAL_OPEN_LIMIT}\n"
        f"Purchased batches: <b>{len(purchased)}</b>\n"
        f"Bonus hours: {user.get('referral_bonus_hours',0)}",
        parse_mode=ParseMode.HTML,
    )


async def _handle_my_access(update, context, user):
    q = update.callback_query
    purchased = user.get("purchased_batches") or []
    if not purchased:
        await _safe_edit(q,
            "⭐ You haven't purchased any batches yet.\nTap Browse Batches to see options.",
            reply_markup=ui.main_menu_kb())
        return
    rows = db.query(
        "SELECT * FROM batches WHERE batch_id = ANY(%s) ORDER BY name",
        (purchased,),
    )
    counts = user.get("paid_open_count") or {}
    text = "⭐ <b>Your Purchased Batches</b>\n\n"
    for b in rows:
        used = int(counts.get(str(b["batch_id"]), 0))
        text += (f"• {b['name']}  —  {used}/{settings.PAID_OPEN_LIMIT} opens\n")
    await _safe_edit(q, text, reply_markup=ui.main_menu_kb())


async def support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📞 <b>Support</b>\nDrop your query below and admin will get back to you soon.",
        parse_mode=ParseMode.HTML,
    )
