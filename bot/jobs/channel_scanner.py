"""Channel scanner — resilient to protected/private channels.

Strategy
────────
1. **Probe top message id.**
   • Try `bot.send_message(channel, ".")` → id → delete. Needs bot post rights.
   • If that fails, ask Telegram for `getChat` and use a generous upper bound
     (chat's `pinned_message.message_id` if present, else 20 000).
2. **Iterate message IDs from `start_from` upward.**  For each id:
   • Try `bot.forward_message(admin_dm, channel, id, disable_notification=True)`.
     If it succeeds → inspect for video → save → delete forwarded copy.
   • If forwarding is blocked (protected content) fall back to
     `bot.copy_message(admin_dm, channel, id)` — we lose caption detection but
     still know the message exists, so we save it as a lecture placeholder.
   • Skip on "message to forward not found" / "message can't be copied".
   • Retry on `RetryAfter`.
3. Stops after `MAX_CONSEC_MISSING` consecutive non-existent ids past `top`.
4. All progress is persisted every message in `scan_jobs.last_message_id_scanned`
   so the job resumes cleanly.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import Bot
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut

from config import settings
from database import db
from utils import groq_parser

logger = logging.getLogger(__name__)

MAX_CONSEC_MISSING = 80          # stop after N holes past `top`
PROGRESS_EVERY_N   = 25          # edit progress message every N iterations
POLL_DELAY_S       = 0.04        # gentle pacing


async def _probe_top(bot: Bot, channel_id: int) -> tuple[int, str]:
    """Return (upper_bound, note).  `note` is a human-readable hint."""
    # 1) attempt post-probe
    try:
        msg = await bot.send_message(channel_id, "🔎")
        try:
            await bot.delete_message(channel_id, msg.message_id)
        except Exception:
            pass
        return msg.message_id, "post-probe"
    except Forbidden:
        pass
    except BadRequest as e:
        logger.info("post-probe failed: %s", e)

    # 2) fall back to pinned / get_chat
    try:
        chat = await bot.get_chat(channel_id)
        if chat.pinned_message:
            return chat.pinned_message.message_id + 200, "pinned+buffer"
    except Exception as e:
        logger.warning("get_chat failed: %s", e)

    # 3) last resort — generous default; user can widen later
    return 20000, "default-cap"


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


async def _save_lecture(chap_id: int, name: str, channel_id: int, mid: int) -> bool:
    """Return True if this insert added a new row (not a dup)."""
    row = db.query(
        "SELECT lecture_id FROM lectures WHERE channel_id=%s AND message_id=%s",
        (channel_id, mid), one=True,
    )
    if row:
        return False
    db.execute(
        """INSERT INTO lectures(chapter_id, name, channel_id, message_id)
           VALUES(%s,%s,%s,%s)
           ON CONFLICT (channel_id, message_id) DO NOTHING""",
        (chap_id, name, channel_id, mid),
    )
    return True


async def run_scan(bot: Bot, notify_admin_id: int, batch_id: int, subject_id: int,
                   channel_id: int, resume: bool = False) -> None:
    # ── boot job row ──
    if resume:
        old = db.query(
            "SELECT * FROM scan_jobs WHERE batch_id=%s AND channel_id=%s "
            "ORDER BY started_at DESC LIMIT 1", (batch_id, channel_id), one=True,
        )
        start_from = (old["last_message_id_scanned"] + 1) if old else 1
    else:
        start_from = 1
    job = db.execute_returning(
        """INSERT INTO scan_jobs(batch_id, subject_id, channel_id, status,
                                 last_message_id_scanned)
           VALUES(%s,%s,%s,'running',%s) RETURNING job_id""",
        (batch_id, subject_id, channel_id, start_from - 1),
    )
    job_id = job["job_id"]

    # ── access check ──
    try:
        await bot.get_chat(channel_id)
    except Exception as e:
        _fail(job_id, f"Bot has no access to channel {channel_id}: {e}")
        await _dm(bot, notify_admin_id,
                  f"❌ Scan aborted — bot cannot access channel <code>{channel_id}</code>.\n"
                  f"Reason: {e}\n\nAdd the bot as <b>admin</b> in the channel and retry.")
        return

    top, hint = await _probe_top(bot, channel_id)
    if top < start_from:
        top = start_from + 5000  # empty-ish channel; give a small window

    progress = await _dm(bot, notify_admin_id,
        f"🛰 Scan #{job_id} starting…\n"
        f"Channel: <code>{channel_id}</code>\n"
        f"Range: {start_from} → {top} (via {hint})")

    total_found = total_new = 0
    forward_blocked = False       # channel protects content → we'll use copy_message
    consec_missing = 0
    mid = start_from

    while True:
        if consec_missing >= MAX_CONSEC_MISSING and mid > top:
            break
        if mid > top + 10_000:      # hard safety cap
            break

        # ── try to fetch metadata via forward_message ──
        fwd = None
        exists = False
        is_video = False
        caption = ""
        if not forward_blocked:
            try:
                fwd = await bot.forward_message(
                    chat_id=notify_admin_id, from_chat_id=channel_id,
                    message_id=mid, disable_notification=True,
                )
                exists = True
                is_video = bool(fwd.video) or bool(
                    fwd.document and (fwd.document.mime_type or "").startswith("video")
                )
                caption = (fwd.caption or
                           (fwd.video.file_name if fwd.video and fwd.video.file_name else "") or
                           (fwd.document.file_name if fwd.document and fwd.document.file_name else "") or
                           "")
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1); continue
            except TimedOut:
                await asyncio.sleep(2); continue
            except Forbidden:
                _fail(job_id, "Forbidden while forwarding.")
                await _dm(bot, notify_admin_id, "❌ Bot lost forward permission — scan aborted.")
                return
            except BadRequest as e:
                err = str(e).lower()
                if "can't be forwarded" in err or "forwarding" in err or "protected" in err:
                    # channel has content protection – switch to copy_message for whole scan
                    forward_blocked = True
                    logger.info("channel %s protects content – switching to copy_message", channel_id)
                elif "not found" in err or "to forward" in err or "empty" in err:
                    consec_missing += 1
                    exists = False
                else:
                    logger.warning("forward mid=%s err=%s", mid, e)
                    consec_missing += 1

        # ── copy_message fallback (metadata-less) ──
        if forward_blocked:
            try:
                cp = await bot.copy_message(
                    chat_id=notify_admin_id, from_chat_id=channel_id,
                    message_id=mid, disable_notification=True,
                )
                exists = True
                is_video = True          # best-effort; user can prune later
                caption = f"Lecture {mid}"
                try:
                    await bot.delete_message(notify_admin_id, cp.message_id)
                except Exception:
                    pass
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1); continue
            except TimedOut:
                await asyncio.sleep(2); continue
            except BadRequest as e:
                err = str(e).lower()
                if "not found" in err or "empty" in err or "can't be copied" in err:
                    consec_missing += 1
                else:
                    logger.warning("copy mid=%s err=%s", mid, e)
                    consec_missing += 1

        # ── record if it's a video ──
        if exists and is_video:
            consec_missing = 0
            total_found += 1
            parsed = groq_parser.parse_caption(caption)
            chap_name = parsed.get("chapter") or "General"
            lec_name = (parsed.get("lecture")
                        or (caption.strip().split("\n")[0][:120] if caption else f"Lecture {mid}"))
            chap_id = await _resolve_chapter(subject_id, chap_name)
            if await _save_lecture(chap_id, lec_name, channel_id, mid):
                total_new += 1
        elif exists:
            consec_missing = 0     # exists but not a video (photo/text) — ignore

        # cleanup forwarded probe copy
        if fwd is not None:
            try:
                await bot.delete_message(notify_admin_id, fwd.message_id)
            except Exception:
                pass

        # persist progress
        db.execute(
            """UPDATE scan_jobs
               SET last_message_id_scanned=%s, total_found=%s, total_processed=%s
               WHERE job_id=%s""",
            (mid, total_found, total_new, job_id),
        )

        if mid % PROGRESS_EVERY_N == 0 and progress is not None:
            try:
                await bot.edit_message_text(
                    chat_id=notify_admin_id, message_id=progress.message_id,
                    text=(f"🛰 Scan #{job_id}\n"
                          f"Progress: <b>{mid}</b> / {top}\n"
                          f"Videos seen: {total_found}  •  New saved: {total_new}\n"
                          f"Missing streak: {consec_missing}\n"
                          f"Mode: {'copy_message (protected)' if forward_blocked else 'forward_message'}"),
                    parse_mode="HTML",
                )
            except Exception:
                pass

        mid += 1
        await asyncio.sleep(POLL_DELAY_S)

    db.execute(
        "UPDATE scan_jobs SET status='completed', completed_at=NOW() WHERE job_id=%s",
        (job_id,),
    )
    await _dm(bot, notify_admin_id,
        f"✅ <b>Scan #{job_id} complete</b>\n"
        f"Range covered: {start_from} → {mid - 1}\n"
        f"Videos seen: <b>{total_found}</b>\n"
        f"New lectures saved: <b>{total_new}</b>\n"
        f"Mode: {'copy_message (protected)' if forward_blocked else 'forward_message'}")


def _fail(job_id: int, msg: str) -> None:
    db.execute(
        "UPDATE scan_jobs SET status='failed', completed_at=NOW(), log=%s WHERE job_id=%s",
        (msg, job_id),
    )


async def _dm(bot: Bot, admin_id: int, text: str):
    try:
        return await bot.send_message(admin_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning("DM to admin failed: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════
#  Real-time channel-post handler — auto-index new videos as they arrive
# ═══════════════════════════════════════════════════════════════════════
async def on_channel_post(update, context) -> None:
    """Auto-catch new videos in ANY channel the bot is admin of.

    Attaches them to the *most recent* scan_jobs row that matches this channel
    (so admin doesn't have to re-run /update_channel for every drop).
    If no scan job exists for the channel yet, silently ignore.
    """
    msg = update.channel_post or update.edited_channel_post
    if msg is None:
        return
    is_video = bool(msg.video) or bool(
        msg.document and (msg.document.mime_type or "").startswith("video")
    )
    if not is_video:
        return
    ch_id = msg.chat.id
    job = db.query(
        "SELECT batch_id, subject_id FROM scan_jobs WHERE channel_id=%s "
        "ORDER BY started_at DESC LIMIT 1", (ch_id,), one=True,
    )
    if not job:
        logger.info("channel_post from %s ignored — no scan job registered", ch_id)
        return
    caption = (msg.caption or
               (msg.video.file_name if msg.video and msg.video.file_name else "") or
               (msg.document.file_name if msg.document and msg.document.file_name else "") or
               f"Lecture {msg.message_id}")
    parsed = groq_parser.parse_caption(caption)
    chap_id = await _resolve_chapter(job["subject_id"], parsed.get("chapter") or "General")
    lec_name = parsed.get("lecture") or caption.strip().split("\n")[0][:120]
    added = await _save_lecture(chap_id, lec_name, ch_id, msg.message_id)
    if added:
        logger.info("auto-indexed lecture ch=%s mid=%s", ch_id, msg.message_id)
