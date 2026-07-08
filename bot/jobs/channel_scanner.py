"""Channel scanner + LIVE CAPTURE mode.

Preferred flow (what the user asked for):
─────────────────────────────────────────
1. Owner adds @Babujiihebot as ADMIN in the channel.
2. Owner posts  `/scan BATCHCODE`  IN THE CHANNEL.
3. Bot DMs the owner "Pick a subject" (buttons over subjects of that batch).
4. Owner taps a subject → capture mode ON for that channel.
5. Owner uploads to the channel:
      🎥 video     → creates a new lecture (name auto-parsed via Groq)
      📄 doc/pdf   → attaches as *Notes* if empty, else as *DPP* of the last video
      🔗 URL text  → same rule as above
   Media groups (video + pdf sent as one album) are auto-grouped.
6. Owner posts `/done` (or `/stop`) in the channel → capture ends,
   bot DMs a summary.

Also keeps the historical scanner (`run_scan`) for old backlogs.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from config import settings
from database import db
from utils import groq_parser

logger = logging.getLogger(__name__)


# ═════════════ LIVE CAPTURE state ═════════════
# channel_id -> {batch_id, subject_id, last_lecture_id, last_media_group_id, started_at, count}
LIVE_CAPTURE: dict[int, dict] = {}


# ═════════════ shared helpers ═════════════
async def _resolve_chapter(subject_id: int, name: Optional[str]) -> int:
    chap = (name or "General").strip() or "General"
    row = db.query(
        "SELECT chapter_id FROM chapters WHERE subject_id=%s AND LOWER(name)=LOWER(%s)",
        (subject_id, chap), one=True,
    )
    if row:
        return row["chapter_id"]
    row = db.execute_returning(
        "INSERT INTO chapters(subject_id, name) VALUES(%s,%s) RETURNING chapter_id",
        (subject_id, chap),
    )
    return row["chapter_id"]


async def _save_lecture(chap_id: int, name: str, channel_id: int, mid: int) -> Optional[int]:
    row = db.query(
        "SELECT lecture_id FROM lectures WHERE channel_id=%s AND message_id=%s",
        (channel_id, mid), one=True,
    )
    if row:
        return row["lecture_id"]
    row = db.execute_returning(
        """INSERT INTO lectures(chapter_id, name, channel_id, message_id)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT (channel_id, message_id) DO NOTHING
           RETURNING lecture_id""",
        (chap_id, name, channel_id, mid),
    )
    return row["lecture_id"] if row else None


def _extract_url(text: str) -> Optional[str]:
    m = re.search(r"https?://\S+", text or "")
    return m.group(0) if m else None


def _classify(msg) -> str:
    if msg.video:
        return "video"
    if msg.document:
        mt = (msg.document.mime_type or "").lower()
        if mt.startswith("video"):
            return "video"
        if mt == "application/pdf" or (msg.document.file_name or "").lower().endswith(".pdf"):
            return "pdf"
        return "doc"
    if msg.text and ("http://" in msg.text or "https://" in msg.text):
        return "url"
    return "other"


def _caption_of(msg) -> str:
    cap = msg.caption or msg.text or ""
    if not cap and msg.video and msg.video.file_name:
        cap = msg.video.file_name
    if not cap and msg.document and msg.document.file_name:
        cap = msg.document.file_name
    return cap.strip()


# ═════════════ LIVE CAPTURE — the workflow user asked for ═════════════
async def _handle_command_in_channel(update, context) -> bool:
    """Detect /scan or /done posted inside a channel. Returns True if handled."""
    msg = update.channel_post or update.edited_channel_post
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return False
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]      # strip @Babujiihebot
    ch_id = msg.chat.id
    bot = context.bot

    if cmd == "/scan":
        if len(parts) < 2:
            await bot.send_message(ch_id, "❗ Usage: <code>/scan BATCHCODE</code>",
                                   parse_mode=ParseMode.HTML)
            return True
        code = parts[1].strip()
        b = db.query("SELECT * FROM batches WHERE batch_code=%s", (code,), one=True)
        if not b:
            await bot.send_message(ch_id, f"❌ Batch code <code>{code}</code> not found.",
                                   parse_mode=ParseMode.HTML)
            return True
        subs = db.query("SELECT * FROM subjects WHERE batch_id=%s ORDER BY name",
                        (b["batch_id"],))
        if not subs:
            await bot.send_message(ch_id,
                f"❌ Batch <b>{b['name']}</b> has no subjects yet. "
                f"Create one via /admin → Subjects.", parse_mode=ParseMode.HTML)
            return True
        # DM every admin with a subject picker
        buttons = [[InlineKeyboardButton(f"📖 {s['name']}",
                     callback_data=f"livecap:{ch_id}:{b['batch_id']}:{s['subject_id']}")]
                   for s in subs]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="livecap:cancel")])
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(admin_id,
                    f"🛰 <b>Live Capture Requested</b>\n\n"
                    f"Channel: <code>{ch_id}</code>\n"
                    f"Batch: <b>{b['name']}</b> (<code>{b['batch_code']}</code>)\n\n"
                    f"Pick the subject to attach uploads to:",
                    reply_markup=InlineKeyboardMarkup(buttons),
                    parse_mode=ParseMode.HTML)
            except Exception as e:
                logger.warning("DM to admin %s failed: %s", admin_id, e)
        await bot.send_message(ch_id,
            f"🛰 Waiting for admin to pick a subject in DM for batch <b>{b['name']}</b>…",
            parse_mode=ParseMode.HTML)
        return True

    if cmd in ("/done", "/stop", "/end"):
        if ch_id in LIVE_CAPTURE:
            cap = LIVE_CAPTURE.pop(ch_id)
            await bot.send_message(ch_id,
                f"✅ <b>Capture ended.</b>\nLectures added this session: <b>{cap.get('count',0)}</b>",
                parse_mode=ParseMode.HTML)
            for admin_id in settings.ADMIN_IDS:
                try:
                    await bot.send_message(admin_id,
                        f"✅ Live capture ended in channel <code>{ch_id}</code>.\n"
                        f"Lectures added: <b>{cap.get('count',0)}</b>",
                        parse_mode=ParseMode.HTML)
                except Exception: pass
        else:
            await bot.send_message(ch_id, "ℹ️ No active capture in this channel.")
        return True

    if cmd == "/status":
        cap = LIVE_CAPTURE.get(ch_id)
        if cap:
            await bot.send_message(ch_id,
                f"🟢 Capture ACTIVE\nLectures so far: <b>{cap.get('count',0)}</b>",
                parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(ch_id, "🔴 Capture OFF. Send /scan BATCHCODE to start.")
        return True

    return False


async def on_livecap_pick(update, context) -> None:
    """Callback handler for `livecap:<channel_id>:<batch_id>:<subject_id>`."""
    q = update.callback_query
    await q.answer()
    if q.data == "livecap:cancel":
        await q.edit_message_text("❌ Cancelled.")
        return
    _, ch_s, batch_s, sub_s = q.data.split(":")
    ch_id, batch_id, subject_id = int(ch_s), int(batch_s), int(sub_s)
    LIVE_CAPTURE[ch_id] = {
        "batch_id": batch_id,
        "subject_id": subject_id,
        "last_lecture_id": None,
        "last_media_group_id": None,
        "started_at": time.time(),
        "count": 0,
        "admin_id": q.from_user.id,
    }
    sub = db.query("SELECT name FROM subjects WHERE subject_id=%s", (subject_id,), one=True)
    await q.edit_message_text(
        f"✅ <b>Live capture ACTIVE</b>\n\n"
        f"Channel: <code>{ch_id}</code>\n"
        f"Subject: <b>{sub['name'] if sub else subject_id}</b>\n\n"
        f"📤 Now upload to the channel:\n"
        f"1️⃣ Post the <b>video</b> (with caption like 'Chapter 3 — L1 Motion')\n"
        f"2️⃣ Post the <b>Notes PDF</b> right after\n"
        f"3️⃣ Post the <b>DPP</b> after that\n\n"
        f"Repeat for each lecture. When done, post <code>/done</code> in the channel.",
        parse_mode=ParseMode.HTML)
    try:
        await context.bot.send_message(ch_id,
            f"✅ <b>Capture ACTIVE</b> → Subject: <b>{sub['name']}</b>.\n"
            f"Start posting: video first, then its Notes PDF, then its DPP.",
            parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ═════════════ real-time indexer (all channel posts) ═════════════
async def on_channel_post(update, context) -> None:
    msg = update.channel_post or update.edited_channel_post
    if msg is None:
        return

    # commands first (/scan, /done, /status)
    if (msg.text or msg.caption or "").lstrip().startswith("/"):
        handled = await _handle_command_in_channel(update, context)
        if handled:
            return

    ch_id = msg.chat.id
    cap = LIVE_CAPTURE.get(ch_id)

    # If no live capture, fall back to old auto-index (last scan_job for channel)
    if cap is None:
        await _fallback_index(update, context)
        return

    kind = _classify(msg)

    # ── Video → new lecture ──
    if kind == "video":
        caption = _caption_of(msg) or f"Lecture {msg.message_id}"
        parsed = groq_parser.parse_caption(caption)
        chap_name = parsed.get("chapter") or "General"
        lec_name = (parsed.get("lecture")
                    or caption.split("\n")[0][:120]
                    or f"Lecture {msg.message_id}")
        chap_id = await _resolve_chapter(cap["subject_id"], chap_name)
        lec_id = await _save_lecture(chap_id, lec_name, ch_id, msg.message_id)
        if lec_id:
            cap["last_lecture_id"] = lec_id
            cap["last_media_group_id"] = msg.media_group_id
            cap["count"] += 1
            logger.info("[livecap] ch=%s video mid=%s -> lecture %s", ch_id, msg.message_id, lec_id)
        return

    # ── Document (PDF), other doc, or url text → attach to last lecture ──
    if kind in ("pdf", "doc", "url"):
        lec_id = cap.get("last_lecture_id")
        if not lec_id:
            logger.info("[livecap] doc/url received before any video – ignoring")
            return

        # If same media_group as last video, attach for sure; else still attach
        if kind in ("pdf", "doc"):
            # store message_id → we'll `copy_message` to user on request
            cur = db.query(
                "SELECT pdf_message_id, dpp_message_id FROM lectures WHERE lecture_id=%s",
                (lec_id,), one=True,
            )
            if not cur["pdf_message_id"]:
                db.execute(
                    "UPDATE lectures SET pdf_message_id=%s WHERE lecture_id=%s",
                    (msg.message_id, lec_id))
                logger.info("[livecap] pdf attached mid=%s -> lecture %s", msg.message_id, lec_id)
            elif not cur["dpp_message_id"]:
                db.execute(
                    "UPDATE lectures SET dpp_message_id=%s WHERE lecture_id=%s",
                    (msg.message_id, lec_id))
                logger.info("[livecap] dpp attached mid=%s -> lecture %s", msg.message_id, lec_id)
            return

        # URL text
        url = _extract_url(msg.text or "")
        if not url:
            return
        cur = db.query(
            "SELECT pdf_link, dpp_link FROM lectures WHERE lecture_id=%s",
            (lec_id,), one=True,
        )
        if not cur["pdf_link"]:
            db.execute("UPDATE lectures SET pdf_link=%s WHERE lecture_id=%s", (url, lec_id))
        elif not cur["dpp_link"]:
            db.execute("UPDATE lectures SET dpp_link=%s WHERE lecture_id=%s", (url, lec_id))


async def _fallback_index(update, context) -> None:
    """When no live capture is active but a scan_job exists for the channel,
    still auto-index new videos so the admin doesn't lose posts."""
    msg = update.channel_post or update.edited_channel_post
    if _classify(msg) != "video":
        return
    ch_id = msg.chat.id
    job = db.query(
        "SELECT batch_id, subject_id FROM scan_jobs WHERE channel_id=%s "
        "ORDER BY started_at DESC LIMIT 1", (ch_id,), one=True,
    )
    if not job:
        return
    caption = _caption_of(msg) or f"Lecture {msg.message_id}"
    parsed = groq_parser.parse_caption(caption)
    chap_id = await _resolve_chapter(job["subject_id"], parsed.get("chapter") or "General")
    name = parsed.get("lecture") or caption.split("\n")[0][:120]
    await _save_lecture(chap_id, name, ch_id, msg.message_id)


# ═════════════ verify-admin helper ═════════════
async def check_channel_access(bot: Bot, channel_id: int) -> str:
    """Human-readable status of the bot's access to a channel."""
    try:
        chat = await bot.get_chat(channel_id)
    except Exception as e:
        return f"❌ Cannot access channel <code>{channel_id}</code>: {e}"
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(channel_id, me.id)
        status = member.status
        can_post = getattr(member, "can_post_messages", None)
        can_delete = getattr(member, "can_delete_messages", None)
        return (f"✅ <b>{chat.title}</b>\n"
                f"Type: {chat.type}\n"
                f"Bot status: <b>{status}</b>\n"
                f"Can post: {can_post}\n"
                f"Can delete: {can_delete}\n\n"
                f"{'✅ Ready for scan' if status in ('administrator','creator') else '⚠️ Bot is NOT admin here'}")
    except Exception as e:
        return f"⚠️ Got chat but member check failed: {e}"


# ═════════════ historical scanner (unchanged from previous version) ═════════════
MAX_CONSEC_MISSING = 80
PROGRESS_EVERY_N   = 25
POLL_DELAY_S       = 0.04


async def _probe_top(bot: Bot, channel_id: int) -> tuple[int, str]:
    try:
        m = await bot.send_message(channel_id, "🔎")
        try: await bot.delete_message(channel_id, m.message_id)
        except Exception: pass
        return m.message_id, "post-probe"
    except Forbidden: pass
    except BadRequest: pass
    try:
        chat = await bot.get_chat(channel_id)
        if chat.pinned_message:
            return chat.pinned_message.message_id + 200, "pinned+buffer"
    except Exception: pass
    return 20000, "default-cap"


async def run_scan(bot: Bot, notify_admin_id: int, batch_id: int, subject_id: int,
                   channel_id: int, resume: bool = False) -> None:
    if resume:
        old = db.query(
            "SELECT * FROM scan_jobs WHERE batch_id=%s AND channel_id=%s "
            "ORDER BY started_at DESC LIMIT 1", (batch_id, channel_id), one=True)
        start_from = (old["last_message_id_scanned"] + 1) if old else 1
    else:
        start_from = 1
    job = db.execute_returning(
        """INSERT INTO scan_jobs(batch_id, subject_id, channel_id, status,
                                 last_message_id_scanned)
           VALUES(%s,%s,%s,'running',%s) RETURNING job_id""",
        (batch_id, subject_id, channel_id, start_from - 1))
    job_id = job["job_id"]

    try:
        await bot.get_chat(channel_id)
    except Exception as e:
        _fail(job_id, f"no access: {e}")
        await _dm(bot, notify_admin_id,
            f"❌ Scan aborted — no access to <code>{channel_id}</code>: {e}")
        return

    top, hint = await _probe_top(bot, channel_id)
    if top < start_from:
        top = start_from + 5000

    progress = await _dm(bot, notify_admin_id,
        f"🛰 Historical scan #{job_id}\nRange: {start_from}→{top} ({hint})")

    total_found = total_new = 0
    forward_blocked = False
    consec_missing = 0
    mid = start_from

    while True:
        if consec_missing >= MAX_CONSEC_MISSING and mid > top: break
        if mid > top + 10000: break

        fwd = None; exists = False; is_video = False; caption = ""
        if not forward_blocked:
            try:
                fwd = await bot.forward_message(
                    chat_id=notify_admin_id, from_chat_id=channel_id,
                    message_id=mid, disable_notification=True)
                exists = True
                is_video = bool(fwd.video) or bool(
                    fwd.document and (fwd.document.mime_type or "").startswith("video"))
                caption = _caption_of(fwd)
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1); continue
            except TimedOut:
                await asyncio.sleep(2); continue
            except Forbidden:
                _fail(job_id, "forbidden"); await _dm(bot, notify_admin_id, "❌ Bot lost access."); return
            except BadRequest as e:
                err = str(e).lower()
                if "can't be forwarded" in err or "forwarding" in err or "protected" in err:
                    forward_blocked = True
                elif "not found" in err or "to forward" in err or "empty" in err:
                    consec_missing += 1
                else:
                    consec_missing += 1

        if forward_blocked:
            try:
                cp = await bot.copy_message(
                    chat_id=notify_admin_id, from_chat_id=channel_id,
                    message_id=mid, disable_notification=True)
                exists = True; is_video = True; caption = f"Lecture {mid}"
                try: await bot.delete_message(notify_admin_id, cp.message_id)
                except Exception: pass
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1); continue
            except TimedOut:
                await asyncio.sleep(2); continue
            except BadRequest:
                consec_missing += 1

        if exists and is_video:
            consec_missing = 0
            total_found += 1
            parsed = groq_parser.parse_caption(caption)
            chap_id = await _resolve_chapter(subject_id, parsed.get("chapter") or "General")
            lec_name = parsed.get("lecture") or (caption.split("\n")[0][:120] or f"Lecture {mid}")
            if await _save_lecture(chap_id, lec_name, channel_id, mid):
                total_new += 1
        elif exists:
            consec_missing = 0

        if fwd is not None:
            try: await bot.delete_message(notify_admin_id, fwd.message_id)
            except Exception: pass

        db.execute(
            "UPDATE scan_jobs SET last_message_id_scanned=%s, total_found=%s, "
            "total_processed=%s WHERE job_id=%s",
            (mid, total_found, total_new, job_id))

        if mid % PROGRESS_EVERY_N == 0 and progress is not None:
            try:
                await bot.edit_message_text(
                    chat_id=notify_admin_id, message_id=progress.message_id,
                    text=(f"🛰 #{job_id}  {mid}/{top}  "
                          f"videos={total_found}  new={total_new}  "
                          f"miss={consec_missing}\n"
                          f"Mode: {'copy' if forward_blocked else 'forward'}"))
            except Exception: pass

        mid += 1
        await asyncio.sleep(POLL_DELAY_S)

    db.execute(
        "UPDATE scan_jobs SET status='completed', completed_at=NOW() WHERE job_id=%s",
        (job_id,))
    await _dm(bot, notify_admin_id,
        f"✅ Scan #{job_id} done. Videos seen: {total_found}  •  New saved: {total_new}")


def _fail(job_id: int, msg: str) -> None:
    db.execute(
        "UPDATE scan_jobs SET status='failed', completed_at=NOW(), log=%s WHERE job_id=%s",
        (msg, job_id))


async def _dm(bot: Bot, admin_id: int, text: str):
    try:
        return await bot.send_message(admin_id, text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning("dm failed: %s", e)
        return None
