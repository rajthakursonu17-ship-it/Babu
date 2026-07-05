"""Channel scanner: exhaustive resumable video message ID collection.

Telegram Bot API doesn't expose a "get channel history" endpoint, BUT if the bot
is an admin in the channel, every new post already fires a channel_post update.
For historical backlog scanning, we walk message IDs incrementally via
`copy_message(from_chat_id=channel, message_id=N)` in "dry run" mode by using
`forwardMessage` to the bot's own private chat (admin) with disable_notification
and then deleting; or preferably `getMessage`... which doesn't exist.

Reliable pattern: iterate a message-id counter, use `bot.forward_message` to the
admin's private chat (silently) to detect existence + video-ness, then store its
metadata, then delete it. Errors ('message not found') are simply skipped.
This is the standard technique for exhaustive PTB scans and is resumable
(persists last_message_id_scanned).
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


async def _get_last_message_id(bot: Bot, channel_id: int) -> int:
    """Best-effort probe: post a temporary message to the channel and use
    its id as an upper bound.  Bot must be admin with post rights."""
    try:
        msg = await bot.send_message(channel_id, "🔎 Shriji scan probe")
        top = msg.message_id
        await bot.delete_message(channel_id, top)
        return top
    except Exception as e:
        logger.warning("scan probe failed: %s", e)
        return 0


async def _resolve_chapter(subject_id: int, chapter_name: Optional[str]) -> int:
    """Get-or-create chapter under given subject."""
    name = (chapter_name or "General").strip() or "General"
    row = db.query(
        "SELECT chapter_id FROM chapters WHERE subject_id=%s AND LOWER(name)=LOWER(%s)",
        (subject_id, name), one=True,
    )
    if row:
        return row["chapter_id"]
    row = db.execute_returning(
        "INSERT INTO chapters(subject_id, name) VALUES(%s,%s) RETURNING chapter_id",
        (subject_id, name),
    )
    return row["chapter_id"]


async def run_scan(bot: Bot, notify_admin_id: int, batch_id: int, subject_id: int,
                   channel_id: int, resume: bool = False) -> None:
    # find or create job row
    if resume:
        job = db.query(
            "SELECT * FROM scan_jobs WHERE batch_id=%s AND channel_id=%s "
            "ORDER BY started_at DESC LIMIT 1",
            (batch_id, channel_id), one=True,
        )
        start_from = (job["last_message_id_scanned"] + 1) if job else 1
    else:
        start_from = 1

    job_row = db.execute_returning(
        """INSERT INTO scan_jobs(batch_id, subject_id, channel_id, status,
                                 last_message_id_scanned)
           VALUES(%s,%s,%s,'running',%s) RETURNING job_id""",
        (batch_id, subject_id, channel_id, start_from - 1),
    )
    job_id = job_row["job_id"]

    top = await _get_last_message_id(bot, channel_id)
    if top < start_from:
        db.execute(
            "UPDATE scan_jobs SET status='failed', log=%s, completed_at=NOW() WHERE job_id=%s",
            ("Bot cannot post to channel (must be admin) or channel empty.", job_id),
        )
        try:
            await bot.send_message(
                notify_admin_id,
                "❌ Scan failed — bot needs admin rights in the channel with post permission.",
            )
        except Exception:
            pass
        return

    total_found = 0
    total_processed = 0
    last_progress_msg = None
    try:
        last_progress_msg = await bot.send_message(
            notify_admin_id, f"🛰 Scan #{job_id} starting… range {start_from}..{top}"
        )
    except Exception:
        pass

    mid = start_from
    consec_missing = 0
    while mid <= top + 20:  # small buffer past `top`
        try:
            fwd = await bot.forward_message(
                chat_id=notify_admin_id,
                from_chat_id=channel_id,
                message_id=mid,
                disable_notification=True,
            )
            consec_missing = 0
            # video only
            if fwd.video or fwd.document and (fwd.document.mime_type or "").startswith("video"):
                caption = fwd.caption or (fwd.video.file_name if fwd.video else "") or f"Lecture {mid}"
                total_found += 1
                parsed = groq_parser.parse_caption(caption)
                chap_name = parsed.get("chapter") or "General"
                lec_name = parsed.get("lecture") or caption.strip().split("\n")[0][:120]
                chap_id = await _resolve_chapter(subject_id, chap_name)
                db.execute(
                    """INSERT INTO lectures(chapter_id, name, channel_id, message_id)
                       VALUES(%s,%s,%s,%s)
                       ON CONFLICT (channel_id, message_id) DO NOTHING""",
                    (chap_id, lec_name, channel_id, mid),
                )
                total_processed += 1
            # cleanup the probe copy from admin chat
            try:
                await bot.delete_message(notify_admin_id, fwd.message_id)
            except Exception:
                pass
        except BadRequest as e:
            # message doesn't exist or not forwardable → skip
            consec_missing += 1
        except Forbidden:
            db.execute(
                "UPDATE scan_jobs SET status='failed', log=%s, completed_at=NOW() WHERE job_id=%s",
                (f"Forbidden at mid={mid}", job_id),
            )
            try: await bot.send_message(notify_admin_id, "❌ Scan aborted — bot lost access.")
            except Exception: pass
            return
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            continue
        except TimedOut:
            await asyncio.sleep(2); continue
        except Exception as e:
            logger.warning("scan mid=%s err=%s", mid, e)

        db.execute(
            """UPDATE scan_jobs
               SET last_message_id_scanned=%s, total_found=%s, total_processed=%s
               WHERE job_id=%s""",
            (mid, total_found, total_processed, job_id),
        )

        if mid % 50 == 0 and last_progress_msg is not None:
            try:
                await bot.edit_message_text(
                    chat_id=notify_admin_id,
                    message_id=last_progress_msg.message_id,
                    text=(f"🛰 Scan #{job_id}\n"
                          f"Progress: {mid}/{top}\n"
                          f"Videos captured: {total_processed}"),
                )
            except Exception:
                pass

        # stop if we've clearly overshot end
        if mid > top and consec_missing >= 30:
            break

        mid += 1
        await asyncio.sleep(0.05)  # gentle pacing

    db.execute(
        "UPDATE scan_jobs SET status='completed', completed_at=NOW() WHERE job_id=%s",
        (job_id,),
    )
    try:
        await bot.send_message(
            notify_admin_id,
            f"✅ <b>Scan #{job_id} complete</b>\n"
            f"Videos captured: <b>{total_processed}</b>\n"
            f"Last message id: {mid - 1}",
            parse_mode="HTML",
        )
    except Exception:
        pass
