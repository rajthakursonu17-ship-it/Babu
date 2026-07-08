"""Fully button-driven admin panel.

Interaction model
─────────────────
• `/admin` → password prompt → main inline menu.
• Every action (add/edit/delete a batch, subject, chapter, lecture, user
  action, broadcast, setting, scan) is triggered by a button.
• When user input is required, the bot asks with a message; the user simply
  types the value in reply.  A single text-router picks up the reply based on
  the admin's current state (`ADMIN_STATE[uid]`).
• "🏠 Menu", "⬅️ Back" and "❌ Cancel" buttons everywhere.
• Only `/give_access`, `/scan`, `/update_channel` remain as *optional*
  shortcut commands (button flows exist for all of them too).
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from typing import Any

import bcrypt
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler,
    MessageHandler, filters,
)

from config import settings
from database import db
from utils import ui_helpers as ui
from utils import link_parser
from jobs import channel_scanner

logger = logging.getLogger(__name__)


# ═════════════ session & per-admin state ═════════════
ADMIN_SESSIONS: set[int] = set()

# ADMIN_STATE[uid] = {"action": "add_batch", "step": "name", "data": {...}}
ADMIN_STATE: dict[int, dict[str, Any]] = {}


def is_admin_user(tg_id: int) -> bool:
    return tg_id in settings.ADMIN_IDS


def is_admin_session(tg_id: int) -> bool:
    return tg_id in ADMIN_SESSIONS


def _reset(uid: int) -> None:
    ADMIN_STATE.pop(uid, None)


def _kb(rows):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


NAV = [("⬅️ Back", "adm:home"), ("❌ Cancel", "adm:cancel")]


# ═════════════ /admin login ═════════════
ASK_PW = 100


async def admin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin_user(update.effective_user.id):
        await update.message.reply_text("🚫 Not authorised.")
        return ConversationHandler.END
    await update.message.reply_text(
        "🔐 <b>Admin Access</b>\nEnter admin password:",
        parse_mode=ParseMode.HTML,
    )
    return ASK_PW


async def admin_password(update, context) -> int:
    if update.message.text.strip() != settings.ADMIN_PASSWORD:
        await update.message.reply_text("❌ Wrong password.")
        return ConversationHandler.END
    ADMIN_SESSIONS.add(update.effective_user.id)
    _reset(update.effective_user.id)
    await update.message.reply_text(
        "✅ <b>Admin Panel Unlocked</b>\nEverything below is one tap away.",
        reply_markup=_home_kb(), parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def admin_cancel(update, context) -> int:
    return ConversationHandler.END


def build_admin_conv():
    return ConversationHandler(
        entry_points=[CommandHandler("admin", admin_entry)],
        states={ASK_PW: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_password)]},
        fallbacks=[CommandHandler("cancel", admin_cancel)],
        conversation_timeout=180,
    )


# ═════════════ keyboards ═════════════
def _home_kb() -> InlineKeyboardMarkup:
    return _kb([
        [("📚 Batches", "adm:sec:batch"), ("📖 Subjects", "adm:sec:subject")],
        [("📝 Chapters", "adm:sec:chapter"), ("🎥 Lectures", "adm:sec:lecture")],
        [("🛰️ Channel Scan", "adm:sec:scan"), ("👥 Users", "adm:sec:users")],
        [("💰 Payments", "adm:sec:pay"), ("📣 Broadcast", "adm:sec:broadcast")],
        [("⚙️ Settings", "adm:sec:settings"), ("🚪 Exit", "adm:exit")],
    ])


def _crud_kb(section: str) -> InlineKeyboardMarkup:
    emoji = {"batch": "📚", "subject": "📖", "chapter": "📝", "lecture": "🎥"}[section]
    rows = [
        [(f"➕ Add {section.title()}", f"adm:add:{section}")],
        [("📋 List", f"adm:list:{section}"), ("✏️ Edit", f"adm:edit:{section}")],
        [("🗑 Delete", f"adm:del:{section}")]
    ]
    if section == "lecture":
        rows[-1].append(("📥 Bulk Add", "adm:bulk:lecture"))
        rows.append([("🔗 Add via Link", "adm:linkadd:lecture"),
                     ("📎 Attach PDF/DPP", "adm:attach:lecture")])
    rows.append([("🏠 Menu", "adm:home")])
    return _kb(rows)


# ═════════════ text-input router ═════════════
async def admin_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles free-form text from an admin, routed by ADMIN_STATE."""
    uid = update.effective_user.id
    if not is_admin_user(uid) or not is_admin_session(uid):
        return
    st = ADMIN_STATE.get(uid)
    if not st:
        return  # nothing pending; ignore
    action = st["action"]
    step = st["step"]
    txt = update.message.text.strip()

    handlers = {
        "add_batch": _step_add_batch,
        "edit_batch": _step_edit_batch,
        "add_subject": _step_add_subject,
        "edit_subject": _step_edit_subject,
        "add_chapter": _step_add_chapter,
        "edit_chapter": _step_edit_chapter,
        "add_lecture": _step_add_lecture,
        "edit_lecture": _step_edit_lecture,
        "bulk_lecture": _step_bulk_lecture,
        "give_access": _step_give_access,
        "broadcast": _step_broadcast,
        "set_setting": _step_set_setting,
        "set_pw": _step_set_pw,
        "scan": _step_scan,
        "check_rights": _step_check_rights,
        "search_user": _step_search_user,
        "linkadd": _step_linkadd,
    }
    fn = handlers.get(action)
    if fn:
        await fn(update, context, st, txt)


async def admin_photo_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Captures a photo as batch cover when in that step."""
    uid = update.effective_user.id
    if not is_admin_user(uid) or not is_admin_session(uid):
        return
    st = ADMIN_STATE.get(uid)
    if not st or st.get("action") not in ("add_batch", "edit_batch") or st.get("step") != "image":
        return
    file_id = update.message.photo[-1].file_id
    st["data"]["image_file_id"] = file_id
    if st["action"] == "add_batch":
        await _finalize_add_batch(update, context, st)
    else:
        await _finalize_edit_batch(update, context, st, "image_file_id", file_id)


async def admin_document_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles a document (PDF) uploaded by admin during an attach_doc flow."""
    uid = update.effective_user.id
    if not is_admin_user(uid) or not is_admin_session(uid):
        return
    st = ADMIN_STATE.get(uid)
    if not st or st.get("action") != "attach_doc":
        return
    doc = update.message.document
    if not doc:
        await update.message.reply_text("Please send it as a document (file).")
        return
    lec_id = st["data"]["lecture_id"]
    slot = st["data"]["slot"]
    field = "pdf_file_id" if slot == "notes" else "dpp_file_id"
    db.execute(f"UPDATE lectures SET {field}=%s WHERE lecture_id=%s",
               (doc.file_id, lec_id))
    _reset(uid)
    await update.message.reply_text(
        f"✅ {slot.upper()} attached to lecture #{lec_id}.",
        reply_markup=_home_kb(),
    )


# ═════════════ callback router ═════════════
async def on_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not is_admin_session(uid):
        await q.edit_message_text("🔒 Session expired. Send /admin again.")
        return
    data = q.data

    if data == "adm:home":
        _reset(uid)
        await _safe_edit(q, "🏠 <b>Admin Menu</b>", _home_kb()); return

    if data == "adm:cancel":
        _reset(uid)
        await _safe_edit(q, "❌ Cancelled.", _home_kb()); return

    if data == "adm:exit":
        ADMIN_SESSIONS.discard(uid); _reset(uid)
        await _safe_edit(q, "🚪 Admin session closed. Send /admin to reopen.", None); return

    # section landing
    if data.startswith("adm:sec:"):
        sec = data.split(":")[2]
        if sec in ("batch", "subject", "chapter", "lecture"):
            emoji = {"batch": "📚", "subject": "📖", "chapter": "📝", "lecture": "🎥"}[sec]
            await _safe_edit(q, f"{emoji} <b>{sec.title()} Management</b>", _crud_kb(sec)); return
        if sec == "scan":
            await _sec_scan(q); return
        if sec == "users":
            await _sec_users(q); return
        if sec == "pay":
            await _sec_pay(q); return
        if sec == "broadcast":
            ADMIN_STATE[uid] = {"action": "broadcast", "step": "text", "data": {}}
            await _safe_edit(q, "📣 <b>Broadcast</b>\nSend the message you'd like to blast "
                             "to all users. It supports HTML formatting.\n\nSend /cancel or "
                             "tap Cancel to abort.", _kb([NAV])); return
        if sec == "settings":
            await _sec_settings(q); return

    # add flow
    if data.startswith("adm:add:"):
        section = data.split(":")[2]
        await _start_add(uid, section, q); return

    # list flow
    if data.startswith("adm:list:"):
        section = data.split(":")[2]
        await _list_section(q, section); return

    # edit flow
    if data.startswith("adm:edit:"):
        section = data.split(":")[2]
        await _start_edit(uid, section, q); return

    # delete flow
    if data.startswith("adm:del:"):
        section = data.split(":")[2]
        await _start_del(uid, section, q); return

    # bulk lecture entry point → pick chapter first
    if data == "adm:bulk:lecture":
        await _pick_chapter_for(uid, q, purpose="bulk"); return

    # attach PDF/DPP to a lecture (admin uploads a doc)
    if data == "adm:attach:lecture":
        await _pick_lecture_for(uid, q, purpose="attach_choose"); return
    if data.startswith("adm:attachslot:"):
        _, _, lid, slot = data.split(":")
        ADMIN_STATE[uid] = {"action": "attach_doc", "step": "upload",
                            "data": {"lecture_id": int(lid), "slot": slot}}
        await _safe_edit(q,
            f"📎 Send the <b>{slot.upper()} PDF</b> to me now (as a document).\n"
            f"It will be attached to lecture #{lid}.",
            _kb([NAV])); return

    # Add lecture by Telegram message link (multi-step)
    if data == "adm:linkadd:lecture":
        await _pick_subject_for(uid, q, purpose="linkadd_pickchap"); return
    if data.startswith("adm:linkadd:chap:"):
        # adm:linkadd:chap:<subject_id>:new  OR  <subject_id>:<chapter_id>
        _, _, _, sub, third = data.split(":")
        sub_id = int(sub)
        if third == "new":
            ADMIN_STATE[uid] = {"action": "linkadd", "step": "chapter_name",
                                "data": {"subject_id": sub_id}}
            await _safe_edit(q, "📝 Send the <b>new chapter name</b>:", _kb([NAV]))
            return
        chap_id = int(third)
        ADMIN_STATE[uid] = {"action": "linkadd", "step": "lecture_name",
                            "data": {"subject_id": sub_id, "chapter_id": chap_id, "count": 0}}
        await _safe_edit(q,
            "🎥 <b>Adding lectures by link</b>\n\n"
            "Step 1/4 — send the <b>lecture name</b> (e.g. 'Lecture 1 – Intro').",
            _kb([NAV]))
        return
    if data == "adm:linkadd:next":
        st = ADMIN_STATE.get(uid) or {}
        if st.get("action") != "linkadd":
            await _safe_edit(q, "Session lost.", _home_kb()); return
        st["step"] = "lecture_name"
        st["data"].pop("current", None)
        await _safe_edit(q, "🎥 Send the <b>next lecture's name</b>:", _kb([NAV]))
        return
    if data == "adm:linkadd:done":
        st = ADMIN_STATE.pop(uid, None) or {}
        count = st.get("data", {}).get("count", 0)
        await _safe_edit(q, f"✅ Done. Added <b>{count}</b> lecture(s).", _home_kb())
        return

    # picker callbacks: adm:pick:<section>:<id>[:<purpose>]
    if data.startswith("adm:pick:"):
        await _handle_pick(uid, q, data); return

    # edit-field selector: adm:field:<section>:<id>:<field>
    if data.startswith("adm:field:"):
        _, _, section, iid, field = data.split(":")
        await _ask_new_value(uid, q, section, int(iid), field); return

    # delete confirm: adm:confirm_del:<section>:<id>
    if data.startswith("adm:confirm_del:"):
        _, _, section, iid = data.split(":")
        await _do_delete(q, section, int(iid)); return

    # users → search / list
    if data == "adm:users:list":
        await _users_list(q); return
    if data == "adm:users:search":
        ADMIN_STATE[uid] = {"action": "search_user", "step": "q", "data": {}}
        await _safe_edit(q, "🔎 Send a name, username or Telegram ID to search.",
                         _kb([NAV])); return

    # pay actions
    if data.startswith("adm:pay:give:"):
        pid = int(data.split(":")[3])
        await _pay_give_start(uid, q, pid); return

    # settings
    if data == "adm:settings:pw":
        ADMIN_STATE[uid] = {"action": "set_pw", "step": "value", "data": {}}
        await _safe_edit(q, "🔑 Send the <b>new admin password</b>.", _kb([NAV])); return
    if data.startswith("adm:settings:key:"):
        key = data.split(":", 3)[3]
        ADMIN_STATE[uid] = {"action": "set_setting", "step": "value",
                            "data": {"key": key}}
        await _safe_edit(q, f"⚙️ Send the new value for <code>{key}</code>.",
                         _kb([NAV])); return

    # scan
    if data == "adm:scan:new":
        ADMIN_STATE[uid] = {"action": "scan", "step": "batch_code",
                            "data": {"resume": False}}
        await _safe_edit(q, "🛰️ Send the <b>batch_code</b> to scan into.", _kb([NAV])); return
    if data == "adm:scan:update":
        ADMIN_STATE[uid] = {"action": "scan", "step": "batch_code",
                            "data": {"resume": True}}
        await _safe_edit(q, "🔄 Send the <b>batch_code</b> for delta scan.", _kb([NAV])); return
    if data == "adm:scan:check":
        ADMIN_STATE[uid] = {"action": "check_rights", "step": "channel_id", "data": {}}
        await _safe_edit(q,
            "🔍 Send the <b>channel_id</b> to verify the bot's admin rights.\n"
            "Tip: forward any message from that channel to me first, then paste the "
            "numeric id shown in it (e.g. <code>-1001234567890</code>).",
            _kb([NAV])); return


async def _safe_edit(q, text, reply_markup=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup,
                                  parse_mode=ParseMode.HTML,
                                  disable_web_page_preview=True)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        try:
            await q.message.reply_text(text, reply_markup=reply_markup,
                                       parse_mode=ParseMode.HTML,
                                       disable_web_page_preview=True)
        except Exception:
            pass


# ═════════════ LIST ═════════════
async def _list_section(q, section: str) -> None:
    if section == "batch":
        rows = db.query("SELECT * FROM batches ORDER BY created_at DESC")
        txt = "📚 <b>All Batches</b>\n\n" + (
            "\n".join(f"#{r['batch_id']} • <b>{r['name']}</b> • "
                     f"<code>{r['batch_code']}</code> • ₹{r['price']}" for r in rows) or "None")
    elif section == "subject":
        rows = db.query(
            "SELECT s.*, b.name AS bname FROM subjects s JOIN batches b ON b.batch_id=s.batch_id "
            "ORDER BY b.name, s.name"
        )
        txt = "📖 <b>All Subjects</b>\n\n" + (
            "\n".join(f"#{r['subject_id']} • <b>{r['name']}</b> (in {r['bname']})" for r in rows) or "None")
    elif section == "chapter":
        rows = db.query(
            "SELECT c.*, s.name AS sname FROM chapters c JOIN subjects s ON s.subject_id=c.subject_id "
            "ORDER BY s.name, c.name"
        )
        txt = "📝 <b>All Chapters</b>\n\n" + (
            "\n".join(f"#{r['chapter_id']} • <b>{r['name']}</b> (in {r['sname']})" for r in rows) or "None")
    elif section == "lecture":
        rows = db.query(
            "SELECT l.*, c.name AS cname FROM lectures l JOIN chapters c ON c.chapter_id=l.chapter_id "
            "ORDER BY l.created_at DESC LIMIT 40"
        )
        txt = "🎥 <b>Latest 40 Lectures</b>\n\n" + (
            "\n".join(f"#{r['lecture_id']} • {r['name']} (in {r['cname']})" for r in rows) or "None")
    await _safe_edit(q, txt[:3800], _crud_kb(section))


# ═════════════ ADD ═════════════
async def _start_add(uid: int, section: str, q) -> None:
    if section == "batch":
        ADMIN_STATE[uid] = {"action": "add_batch", "step": "name", "data": {}}
        await _safe_edit(q, "📚 <b>New Batch</b>\nStep 1/4 — send the batch <b>name</b>.",
                         _kb([NAV])); return
    if section == "subject":
        await _pick_batch_for(uid, q, purpose="add_subject"); return
    if section == "chapter":
        await _pick_subject_for(uid, q, purpose="add_chapter"); return
    if section == "lecture":
        await _pick_chapter_for(uid, q, purpose="add_lecture"); return


# ---------- batch add steps ----------
async def _step_add_batch(update, context, st, txt: str) -> None:
    step = st["step"]
    if step == "name":
        st["data"]["name"] = txt; st["step"] = "description"
        await update.message.reply_text("Step 2/4 — send a short <b>description</b> (or '-' to skip).",
                                        parse_mode=ParseMode.HTML, reply_markup=_kb([NAV])); return
    if step == "description":
        st["data"]["description"] = "" if txt == "-" else txt
        st["step"] = "price"
        await update.message.reply_text("Step 3/4 — send the <b>price</b> in ₹ (numbers only).",
                                        parse_mode=ParseMode.HTML, reply_markup=_kb([NAV])); return
    if step == "price":
        try:
            st["data"]["price"] = float(txt)
        except ValueError:
            await update.message.reply_text("Price must be a number. Try again.")
            return
        st["step"] = "image"
        await update.message.reply_text(
            "Step 4/4 — send a <b>cover photo</b> for this batch\n"
            "(or type <code>skip</code> to leave it blank).",
            parse_mode=ParseMode.HTML, reply_markup=_kb([NAV]))
        return
    if step == "image":
        if txt.lower() == "skip":
            await _finalize_add_batch(update, context, st)


async def _finalize_add_batch(update, context, st) -> None:
    d = st["data"]
    code = "B" + secrets.token_hex(3).upper()
    row = db.execute_returning(
        """INSERT INTO batches(name, description, price, image_file_id, batch_code)
           VALUES(%s,%s,%s,%s,%s) RETURNING batch_id, batch_code""",
        (d["name"], d.get("description", ""), d.get("price", 0),
         d.get("image_file_id"), code),
    )
    _reset(update.effective_user.id)
    await update.message.reply_text(
        f"✅ <b>Batch Created</b>\n\n"
        f"Name: <b>{d['name']}</b>\n"
        f"ID: <code>{row['batch_id']}</code>\n"
        f"Code: <code>{row['batch_code']}</code>\n"
        f"Price: ₹{d.get('price',0)}",
        parse_mode=ParseMode.HTML, reply_markup=_home_kb(),
    )


# ---------- subject add ----------
async def _step_add_subject(update, context, st, txt: str) -> None:
    row = db.execute_returning(
        "INSERT INTO subjects(batch_id, name) VALUES(%s,%s) RETURNING subject_id",
        (st["data"]["batch_id"], txt),
    )
    _reset(update.effective_user.id)
    await update.message.reply_text(f"✅ Subject #{row['subject_id']} created.",
                                    reply_markup=_home_kb())


# ---------- chapter add ----------
async def _step_add_chapter(update, context, st, txt: str) -> None:
    row = db.execute_returning(
        "INSERT INTO chapters(subject_id, name) VALUES(%s,%s) RETURNING chapter_id",
        (st["data"]["subject_id"], txt),
    )
    _reset(update.effective_user.id)
    await update.message.reply_text(f"✅ Chapter #{row['chapter_id']} created.",
                                    reply_markup=_home_kb())


# ---------- lecture add ----------
async def _step_add_lecture(update, context, st, txt: str) -> None:
    d = st["data"]; step = st["step"]
    if step == "name":
        d["name"] = txt; st["step"] = "channel_id"
        await update.message.reply_text(
            "Send <b>channel_id</b> (numeric, e.g. -1001234567890) or '-' to skip.",
            parse_mode=ParseMode.HTML, reply_markup=_kb([NAV])); return
    if step == "channel_id":
        d["channel_id"] = None if txt == "-" else int(txt)
        st["step"] = "message_id"
        await update.message.reply_text("Send video <b>message_id</b> (integer) or '-' to skip.",
                                        parse_mode=ParseMode.HTML, reply_markup=_kb([NAV])); return
    if step == "message_id":
        d["message_id"] = None if txt == "-" else int(txt)
        st["step"] = "pdf"
        await update.message.reply_text("Send <b>PDF link</b> or '-' to skip.",
                                        parse_mode=ParseMode.HTML, reply_markup=_kb([NAV])); return
    if step == "pdf":
        d["pdf_link"] = None if txt == "-" else txt
        st["step"] = "dpp"
        await update.message.reply_text("Send <b>DPP link</b> or '-' to skip.",
                                        parse_mode=ParseMode.HTML, reply_markup=_kb([NAV])); return
    if step == "dpp":
        d["dpp_link"] = None if txt == "-" else txt
        row = db.execute_returning(
            """INSERT INTO lectures(chapter_id,name,channel_id,message_id,pdf_link,dpp_link)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT (channel_id, message_id)
                 DO UPDATE SET name=EXCLUDED.name, pdf_link=EXCLUDED.pdf_link,
                               dpp_link=EXCLUDED.dpp_link
               RETURNING lecture_id""",
            (d["chapter_id"], d["name"], d["channel_id"], d["message_id"],
             d["pdf_link"], d["dpp_link"]),
        )
        _reset(update.effective_user.id)
        await update.message.reply_text(f"✅ Lecture #{row['lecture_id']} saved.",
                                        reply_markup=_home_kb())


# ---------- bulk lecture ----------
async def _step_bulk_lecture(update, context, st, txt: str) -> None:
    if txt.lower() in ("/done", "done", "finish"):
        _reset(update.effective_user.id)
        await update.message.reply_text("✅ Bulk mode closed.", reply_markup=_home_kb()); return
    ch_id = st["data"]["chapter_id"]
    default_channel = settings.CHANNEL_IDS[0] if settings.CHANNEL_IDS else None
    added = 0
    for line in txt.splitlines():
        line = line.strip()
        if not line: continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2: continue
        while len(parts) < 5: parts.append(None)
        name, mid, pdf, dpp, cch = parts[:5]
        db.execute(
            """INSERT INTO lectures(chapter_id,name,channel_id,message_id,pdf_link,dpp_link)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT (channel_id, message_id) DO NOTHING""",
            (ch_id, name, cch or default_channel, mid or None, pdf or None, dpp or None),
        )
        added += 1
    await update.message.reply_text(
        f"➕ Added {added} lines. Send more, or type <code>done</code> to finish.",
        parse_mode=ParseMode.HTML, reply_markup=_kb([[("✅ Done", "adm:home")]]))


# ═════════════ EDIT & DELETE (button pickers) ═════════════
async def _pick_batch_for(uid, q, purpose: str) -> None:
    rows = db.query("SELECT batch_id, name FROM batches ORDER BY name")
    if not rows:
        await _safe_edit(q, "No batches yet.", _home_kb()); return
    buttons = [[(f"📚 {r['name']}", f"adm:pick:batch:{r['batch_id']}:{purpose}")] for r in rows]
    buttons.append(NAV)
    await _safe_edit(q, f"📚 Pick a batch for <b>{purpose}</b>:", _kb(buttons))


async def _pick_subject_for(uid, q, purpose: str) -> None:
    rows = db.query(
        "SELECT s.subject_id, s.name, b.name AS bname FROM subjects s "
        "JOIN batches b ON b.batch_id=s.batch_id ORDER BY b.name, s.name"
    )
    if not rows:
        await _safe_edit(q, "No subjects yet.", _home_kb()); return
    buttons = [[(f"📖 {r['name']} ({r['bname']})", f"adm:pick:subject:{r['subject_id']}:{purpose}")]
               for r in rows]
    buttons.append(NAV)
    await _safe_edit(q, f"📖 Pick a subject for <b>{purpose}</b>:", _kb(buttons))


async def _pick_chapter_for(uid, q, purpose: str) -> None:
    rows = db.query(
        "SELECT c.chapter_id, c.name, s.name AS sname FROM chapters c "
        "JOIN subjects s ON s.subject_id=c.subject_id ORDER BY s.name, c.name"
    )
    if not rows:
        await _safe_edit(q, "No chapters yet.", _home_kb()); return
    buttons = [[(f"📝 {r['name']} ({r['sname']})", f"adm:pick:chapter:{r['chapter_id']}:{purpose}")]
               for r in rows]
    buttons.append(NAV)
    await _safe_edit(q, f"📝 Pick a chapter for <b>{purpose}</b>:", _kb(buttons))


async def _pick_lecture_for(uid, q, purpose: str) -> None:
    rows = db.query(
        "SELECT lecture_id, name FROM lectures ORDER BY created_at DESC LIMIT 60"
    )
    if not rows:
        await _safe_edit(q, "No lectures yet.", _home_kb()); return
    buttons = [[(f"🎥 {r['name'][:40]}", f"adm:pick:lecture:{r['lecture_id']}:{purpose}")]
               for r in rows]
    buttons.append(NAV)
    await _safe_edit(q, f"🎥 Pick a lecture for <b>{purpose}</b>:", _kb(buttons))


async def _start_edit(uid, section: str, q) -> None:
    if section == "batch":     await _pick_batch_for(uid, q, "edit_batch")
    elif section == "subject": await _pick_subject_for(uid, q, "edit_subject")
    elif section == "chapter": await _pick_chapter_for(uid, q, "edit_chapter")
    elif section == "lecture": await _pick_lecture_for(uid, q, "edit_lecture")


async def _start_del(uid, section: str, q) -> None:
    if section == "batch":     await _pick_batch_for(uid, q, "del_batch")
    elif section == "subject": await _pick_subject_for(uid, q, "del_subject")
    elif section == "chapter": await _pick_chapter_for(uid, q, "del_chapter")
    elif section == "lecture": await _pick_lecture_for(uid, q, "del_lecture")


async def _handle_pick(uid, q, data: str) -> None:
    # adm:pick:<section>:<id>:<purpose>
    parts = data.split(":")
    section, iid, purpose = parts[2], int(parts[3]), parts[4]

    # add flows
    if purpose == "add_subject":
        ADMIN_STATE[uid] = {"action": "add_subject", "step": "name",
                            "data": {"batch_id": iid}}
        await _safe_edit(q, "📖 Send the <b>subject name</b>.", _kb([NAV])); return
    if purpose == "add_chapter":
        ADMIN_STATE[uid] = {"action": "add_chapter", "step": "name",
                            "data": {"subject_id": iid}}
        await _safe_edit(q, "📝 Send the <b>chapter name</b>.", _kb([NAV])); return
    if purpose == "add_lecture":
        ADMIN_STATE[uid] = {"action": "add_lecture", "step": "name",
                            "data": {"chapter_id": iid}}
        await _safe_edit(q, "🎥 Send the <b>lecture name</b>.", _kb([NAV])); return
    if purpose == "bulk":
        ADMIN_STATE[uid] = {"action": "bulk_lecture", "step": "lines",
                            "data": {"chapter_id": iid}}
        default_channel = settings.CHANNEL_IDS[0] if settings.CHANNEL_IDS else "-"
        await _safe_edit(q,
            f"📥 <b>Bulk Add Lectures</b>\n\n"
            f"Send lines (one per lecture):\n"
            f"<code>name|message_id|pdf|dpp|channel_id(optional)</code>\n\n"
            f"Default channel: <code>{default_channel}</code>\n"
            f"Type <code>done</code> to finish.",
            _kb([[("✅ Done", "adm:home")]])); return

    if purpose == "attach_choose":
        # `iid` here is the lecture_id
        lec = db.query(
            "SELECT lecture_id, name, pdf_message_id, dpp_message_id, "
            "       pdf_file_id, dpp_file_id "
            "FROM lectures WHERE lecture_id=%s", (iid,), one=True,
        )
        if not lec:
            await _safe_edit(q, "Lecture not found.", _home_kb()); return
        pdf_set = bool(lec["pdf_message_id"] or lec["pdf_file_id"])
        dpp_set = bool(lec["dpp_message_id"] or lec["dpp_file_id"])
        await _safe_edit(q,
            f"📎 <b>{lec['name']}</b>\n\n"
            f"Notes PDF: {'✅ set' if pdf_set else '❌ empty'}\n"
            f"DPP PDF:   {'✅ set' if dpp_set else '❌ empty'}\n\n"
            f"Which slot to fill?",
            _kb([
                [("📄 Attach Notes",  f"adm:attachslot:{iid}:notes")],
                [("🧪 Attach DPP",    f"adm:attachslot:{iid}:dpp")],
                [("🏠 Menu", "adm:home")],
            ]))
        return

    if purpose == "linkadd_pickchap":
        # `iid` is the subject_id
        chaps = db.query(
            "SELECT chapter_id, name FROM chapters WHERE subject_id=%s ORDER BY name",
            (iid,),
        )
        rows = [[(f"📝 {c['name']}", f"adm:linkadd:chap:{iid}:{c['chapter_id']}")]
                for c in chaps]
        rows.append([("➕ New Chapter", f"adm:linkadd:chap:{iid}:new")])
        rows.append(NAV)
        await _safe_edit(q,
            "📝 Pick an existing chapter, or create a new one:", _kb(rows))
        return

    # edit flow — show field picker
    if purpose.startswith("edit_"):
        await _edit_field_picker(uid, q, section, iid); return

    # delete flow — confirm
    if purpose.startswith("del_"):
        await _confirm_delete(q, section, iid); return


FIELDS = {
    "batch":   [("Name", "name"), ("Description", "description"),
                ("Price", "price"), ("Cover Image", "image_file_id")],
    "subject": [("Name", "name")],
    "chapter": [("Name", "name")],
    "lecture": [("Name", "name"), ("Channel ID", "channel_id"),
                ("Message ID", "message_id"), ("PDF link", "pdf_link"),
                ("DPP link", "dpp_link")],
}


async def _edit_field_picker(uid, q, section: str, iid: int) -> None:
    rows = [[(f"✏️ {label}", f"adm:field:{section}:{iid}:{col}")]
            for label, col in FIELDS[section]]
    rows.append(NAV)
    await _safe_edit(q, f"✏️ Which field of <b>{section}</b> #{iid} do you want to edit?",
                     _kb(rows))


async def _ask_new_value(uid, q, section: str, iid: int, field: str) -> None:
    if section == "batch" and field == "image_file_id":
        ADMIN_STATE[uid] = {"action": "edit_batch", "step": "image",
                            "data": {"batch_id": iid}}
        await _safe_edit(q, "🖼 Send the new <b>cover photo</b>.", _kb([NAV])); return

    action = f"edit_{section}"
    ADMIN_STATE[uid] = {"action": action, "step": "value",
                        "data": {"id": iid, "field": field}}
    await _safe_edit(q, f"Send the new value for <b>{field}</b>:", _kb([NAV]))


async def _step_edit_batch(update, context, st, txt: str) -> None:
    if st["step"] == "image":
        # If user typed 'skip', keep old; otherwise wait for photo
        if txt.lower() == "skip":
            _reset(update.effective_user.id)
            await update.message.reply_text("Kept existing image.", reply_markup=_home_kb())
        return
    await _finalize_edit_generic(update, "batches", "batch_id", st, txt)


async def _finalize_edit_batch(update, context, st, field: str, value: Any) -> None:
    bid = st["data"]["batch_id"]
    db.execute(f"UPDATE batches SET {field}=%s, updated_at=NOW() WHERE batch_id=%s",
               (value, bid))
    _reset(update.effective_user.id)
    await update.message.reply_text("✅ Updated.", reply_markup=_home_kb())


async def _step_edit_subject(update, context, st, txt: str) -> None:
    await _finalize_edit_generic(update, "subjects", "subject_id", st, txt)


async def _step_edit_chapter(update, context, st, txt: str) -> None:
    await _finalize_edit_generic(update, "chapters", "chapter_id", st, txt)


async def _step_edit_lecture(update, context, st, txt: str) -> None:
    await _finalize_edit_generic(update, "lectures", "lecture_id", st, txt)


async def _finalize_edit_generic(update, table: str, pk: str, st, txt: str) -> None:
    field = st["data"]["field"]; iid = st["data"]["id"]
    value: Any = txt
    if field == "price":
        try: value = float(txt)
        except ValueError:
            await update.message.reply_text("Price must be a number.")
            return
    if field in ("channel_id", "message_id"):
        try: value = int(txt)
        except ValueError:
            await update.message.reply_text("Must be an integer.")
            return
    db.execute(f"UPDATE {table} SET {field}=%s WHERE {pk}=%s", (value, iid))
    _reset(update.effective_user.id)
    await update.message.reply_text("✅ Updated.", reply_markup=_home_kb())


async def _confirm_delete(q, section: str, iid: int) -> None:
    labels = {"batch": "📚 Batch", "subject": "📖 Subject",
              "chapter": "📝 Chapter", "lecture": "🎥 Lecture"}
    await _safe_edit(q,
        f"⚠️ Delete {labels[section]} #{iid}?\nThis cascades to children.",
        _kb([[("🗑 Yes, delete", f"adm:confirm_del:{section}:{iid}")],
             [("❌ Cancel", "adm:home")]]))


async def _do_delete(q, section: str, iid: int) -> None:
    table, pk = {
        "batch":   ("batches",   "batch_id"),
        "subject": ("subjects",  "subject_id"),
        "chapter": ("chapters",  "chapter_id"),
        "lecture": ("lectures",  "lecture_id"),
    }[section]
    db.execute(f"DELETE FROM {table} WHERE {pk}=%s", (iid,))
    await _safe_edit(q, "🗑 Deleted.", _home_kb())


# ═════════════ USERS ═════════════
async def _sec_users(q) -> None:
    row = db.query("SELECT COUNT(*) c FROM users", one=True)
    await _safe_edit(q,
        f"👥 <b>Users</b>  •  Total: <b>{row['c']}</b>",
        _kb([[("📋 List latest 30", "adm:users:list"),
              ("🔎 Search", "adm:users:search")],
             [("🏠 Menu", "adm:home")]]))


async def _users_list(q) -> None:
    rows = db.query(
        "SELECT telegram_id, full_name, telegram_username, joined_at, "
        "cardinality(purchased_batches) AS pb FROM users ORDER BY joined_at DESC LIMIT 30"
    )
    txt = "👥 <b>Users (latest 30)</b>\n\n"
    for r in rows:
        txt += (f"• {r['full_name'] or '-'} @{r['telegram_username'] or '-'} "
                f"<code>{r['telegram_id']}</code> batches:{r['pb']}\n")
    await _safe_edit(q, txt[:3800], _kb([[("🏠 Menu", "adm:home")]]))


async def _step_search_user(update, context, st, txt: str) -> None:
    q = "%" + txt + "%"
    rows = db.query(
        "SELECT telegram_id, full_name, telegram_username FROM users "
        "WHERE full_name ILIKE %s OR telegram_username ILIKE %s "
        "OR CAST(telegram_id AS TEXT) ILIKE %s LIMIT 30",
        (q, q, q),
    )
    _reset(update.effective_user.id)
    if not rows:
        await update.message.reply_text("No match.", reply_markup=_home_kb()); return
    text = "\n".join(
        f"{r['full_name'] or '-'} @{r['telegram_username'] or '-'} <code>{r['telegram_id']}</code>"
        for r in rows
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=_home_kb())


# ═════════════ PAYMENTS ═════════════
async def _sec_pay(q) -> None:
    rows = db.query(
        "SELECT p.*, b.name AS bname FROM pending_payments p "
        "JOIN batches b ON b.batch_id=p.batch_id "
        "WHERE p.status='pending' ORDER BY p.created_at DESC LIMIT 15"
    )
    if not rows:
        await _safe_edit(q, "💰 No pending requests.", _kb([[("🏠 Menu", "adm:home")]])); return
    txt = "💰 <b>Pending Payments</b>\n\n"
    buttons = []
    for r in rows:
        txt += f"• req #{r['id']} • <code>{r['telegram_id']}</code> → {r['bname']}\n"
        buttons.append([(f"✅ Approve #{r['id']}", f"adm:pay:give:{r['id']}")])
    buttons.append([("🏠 Menu", "adm:home")])
    await _safe_edit(q, txt, _kb(buttons))


async def _pay_give_start(uid, q, pid: int) -> None:
    r = db.query(
        "SELECT p.*, b.name AS bname FROM pending_payments p "
        "JOIN batches b ON b.batch_id=p.batch_id WHERE p.id=%s", (pid,), one=True)
    if not r:
        await _safe_edit(q, "Request not found.", _home_kb()); return
    ADMIN_STATE[uid] = {"action": "give_access", "step": "username",
                        "data": {"telegram_id": r["telegram_id"],
                                 "batch_id": r["batch_id"],
                                 "pid": r["id"],
                                 "bname": r["bname"]}}
    await _safe_edit(q,
        f"🔐 <b>Grant access</b> to <code>{r['telegram_id']}</code> for <b>{r['bname']}</b>.\n\n"
        f"Send the <b>username</b> for this student:",
        _kb([NAV]))


async def _step_give_access(update, context, st, txt: str) -> None:
    d = st["data"]
    if st["step"] == "username":
        d["username"] = txt; st["step"] = "password"
        await update.message.reply_text("Now send the <b>password</b>.",
                                        parse_mode=ParseMode.HTML, reply_markup=_kb([NAV])); return
    if st["step"] == "password":
        d["password"] = txt
        await _finalize_give_access(update, context, st)


async def _finalize_give_access(update, context, st) -> None:
    d = st["data"]
    tg_id = int(d["telegram_id"]); bid = int(d["batch_id"])
    row = db.query("SELECT * FROM users WHERE telegram_id=%s", (tg_id,), one=True)
    if not row:
        _reset(update.effective_user.id)
        await update.message.reply_text("❌ User hasn't /start-ed the bot.",
                                        reply_markup=_home_kb()); return
    hashed = bcrypt.hashpw(d["password"].encode(), bcrypt.gensalt()).decode()
    current = list(row.get("purchased_batches") or [])
    if bid not in current:
        current.append(bid)
    db.execute(
        "UPDATE users SET edu_username=%s, edu_password=%s, purchased_batches=%s "
        "WHERE telegram_id=%s",
        (d["username"], hashed, current, tg_id),
    )
    if d.get("pid"):
        db.execute("UPDATE pending_payments SET status='confirmed' WHERE id=%s", (d["pid"],))
    try:
        await context.bot.send_message(
            tg_id,
            f"🎉 <b>Access Granted!</b>\n\n"
            f"📚 Batch: <b>{d['bname']}</b>\n\n"
            f"🔐 <b>Your Credentials</b>\n"
            f"Username: <code>{d['username']}</code>\n"
            f"Password: <code>{d['password']}</code>\n\n"
            f"⚠️ Keep them safe.  You now have "
            f"<b>{settings.PAID_OPEN_LIMIT}</b> opens for this batch.",
            parse_mode=ParseMode.HTML,
        )
        _reset(update.effective_user.id)
        await update.message.reply_text("✅ Access granted and credentials sent.",
                                        reply_markup=_home_kb())
    except Exception as e:
        _reset(update.effective_user.id)
        await update.message.reply_text(f"⚠️ Saved, but couldn't DM user: {e}",
                                        reply_markup=_home_kb())


# ═════════════ SETTINGS ═════════════
async def _sec_settings(q) -> None:
    keys = ["FREE_TRIAL_HOURS", "FREE_TRIAL_OPEN_LIMIT", "PAID_OPEN_LIMIT",
            "LECTURE_DELETE_AFTER_HOURS", "SLIDING_WINDOW_SIZE", "REFER_BONUS_HOURS"]
    rows = []
    txt = "⚙️ <b>Settings</b>\n\n"
    for k in keys:
        val = getattr(settings, k)
        txt += f"• <b>{k}</b>: <code>{val}</code>\n"
        rows.append([(f"✏️ {k}", f"adm:settings:key:{k}")])
    rows.append([("🔑 Change Admin Password", "adm:settings:pw")])
    rows.append([("🏠 Menu", "adm:home")])
    await _safe_edit(q, txt, _kb(rows))


async def _step_set_setting(update, context, st, txt: str) -> None:
    key = st["data"]["key"]
    try:
        int(txt)
    except ValueError:
        await update.message.reply_text("Value must be an integer.")
        return
    db.execute(
        "INSERT INTO settings(key,value) VALUES(%s,%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (key, txt))
    setattr(settings, key, int(txt))
    _reset(update.effective_user.id)
    await update.message.reply_text(f"✅ {key} set to {txt}.", reply_markup=_home_kb())


async def _step_set_pw(update, context, st, txt: str) -> None:
    settings.ADMIN_PASSWORD = txt
    db.execute(
        "INSERT INTO settings(key,value) VALUES('ADMIN_PASSWORD',%s) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value", (txt,))
    _reset(update.effective_user.id)
    await update.message.reply_text("✅ Admin password updated.", reply_markup=_home_kb())


# ═════════════ BROADCAST ═════════════
async def _step_broadcast(update, context, st, txt: str) -> None:
    _reset(update.effective_user.id)
    ids = [r["telegram_id"] for r in db.query("SELECT telegram_id FROM users")]
    sent = failed = 0
    for tid in ids:
        try:
            await context.bot.send_message(tid, txt, parse_mode=ParseMode.HTML)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.03)
    await update.message.reply_text(
        f"📣 Broadcast complete.\nSent: {sent} • Failed: {failed}",
        reply_markup=_home_kb(),
    )


# ═════════════ SCAN ═════════════
async def _sec_scan(q) -> None:
    jobs = db.query("SELECT * FROM scan_jobs ORDER BY started_at DESC LIMIT 5")
    txt = "🛰️ <b>Channel Scan</b>\n\n"
    txt += (
        "🎬 <b>Recommended: Live Capture</b>\n"
        "1. Add <b>@Babujiihebot</b> as <b>admin</b> in your channel\n"
        "2. Post <code>/scan BATCHCODE</code> <i>inside the channel</i>\n"
        "3. Pick subject in the DM I send you\n"
        "4. Upload video → PDF (Notes) → PDF (DPP) → repeat\n"
        "5. Post <code>/done</code> in channel when finished\n\n"
        "The bot auto-groups every video with its next 2 documents "
        "as Notes + DPP, and names each lecture from the caption.\n\n"
        "<b>Recent jobs:</b>\n"
    )
    if not jobs:
        txt += "None yet."
    for j in jobs:
        txt += f"#{j['job_id']} • {j['status']} • {j['total_processed']}/{j['total_found']}\n"
    await _safe_edit(q, txt,
        _kb([[("🔍 Check Bot Rights", "adm:scan:check")],
             [("🆕 Historical Full Scan", "adm:scan:new"),
              ("🔄 Delta Scan", "adm:scan:update")],
             [("🏠 Menu", "adm:home")]]))


async def _step_scan(update, context, st, txt: str) -> None:
    d = st["data"]; step = st["step"]
    if step == "batch_code":
        b = db.query("SELECT * FROM batches WHERE batch_code=%s", (txt,), one=True)
        if not b:
            await update.message.reply_text("❌ Batch code not found. Try again or Cancel.",
                                            reply_markup=_kb([NAV])); return
        d["batch_id"] = b["batch_id"]; d["batch_name"] = b["name"]
        st["step"] = "channel_id"
        await update.message.reply_text(
            "Send the <b>channel_id</b> (numeric, e.g. -1001234567890).",
            parse_mode=ParseMode.HTML, reply_markup=_kb([NAV])); return
    if step == "channel_id":
        try:
            d["channel_id"] = int(txt)
        except ValueError:
            await update.message.reply_text("channel_id must be an integer.")
            return
        # pick a subject via keyboard
        subs = db.query("SELECT * FROM subjects WHERE batch_id=%s", (d["batch_id"],))
        if not subs:
            await update.message.reply_text(
                "❌ No subjects in this batch. Create one first (📖 Subjects → Add).",
                reply_markup=_home_kb())
            _reset(update.effective_user.id); return
        buttons = [[InlineKeyboardButton(f"📖 {s['name']}",
                     callback_data=f"adm:pick:subject:{s['subject_id']}:scan_pick")]
                   for s in subs]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="adm:cancel")])
        st["step"] = "waiting_subject_pick"
        await update.message.reply_text("Pick a subject to attach scanned lectures to:",
                                        reply_markup=InlineKeyboardMarkup(buttons))
        return

# ═════════════ Add lectures by Telegram message LINK ═════════════
async def _step_linkadd(update, context, st, txt: str) -> None:
    d = st["data"]; step = st["step"]
    kb_nav = _kb([NAV])

    if step == "chapter_name":
        row = db.execute_returning(
            "INSERT INTO chapters(subject_id, name) VALUES(%s,%s) RETURNING chapter_id",
            (d["subject_id"], txt))
        d["chapter_id"] = row["chapter_id"]
        d["count"] = 0
        st["step"] = "lecture_name"
        await update.message.reply_text(
            f"✅ Chapter '{txt}' created (#{row['chapter_id']}).\n\n"
            f"🎥 Step 1/4 — send the <b>lecture name</b>:",
            parse_mode=ParseMode.HTML, reply_markup=kb_nav)
        return

    if step == "lecture_name":
        d["current"] = {"name": txt}
        st["step"] = "video_link"
        await update.message.reply_text(
            "🎥 Step 2/4 — send the <b>video message link</b>\n"
            "(e.g. <code>https://t.me/c/1234567890/45</code>).\n"
            "Type '-' to skip (video-less lecture).",
            parse_mode=ParseMode.HTML, reply_markup=kb_nav)
        return

    if step == "video_link":
        cur = d.get("current", {})
        cur["channel_id"] = None; cur["message_id"] = None
        if txt != "-":
            ch, mid = link_parser.parse_message_link(txt)
            if ch is None:
                await update.message.reply_text(
                    "❌ Couldn't parse that link.\nSend one like\n"
                    "<code>https://t.me/c/1234567890/45</code>\nor '-' to skip.",
                    parse_mode=ParseMode.HTML)
                return
            if isinstance(ch, str):
                await update.message.reply_text(
                    "⚠️ Public channel link detected. This bot needs a private "
                    "channel link in the form <code>https://t.me/c/…</code>. "
                    "Forward any message from that channel to me first to get its id, "
                    "then use the c-style link.",
                    parse_mode=ParseMode.HTML)
                return
            cur["channel_id"] = ch; cur["message_id"] = mid
        st["step"] = "pdf_link"
        await update.message.reply_text(
            "📄 Step 3/4 — send the <b>Notes PDF link</b> "
            "(t.me link or any http(s) URL), or '-' to skip.",
            parse_mode=ParseMode.HTML, reply_markup=kb_nav)
        return

    if step in ("pdf_link", "dpp_link"):
        cur = d["current"]
        slot_msg = "pdf_message_id" if step == "pdf_link" else "dpp_message_id"
        slot_link = "pdf_link" if step == "pdf_link" else "dpp_link"
        if txt != "-":
            ch, mid = link_parser.parse_message_link(txt)
            if ch is not None and isinstance(ch, int):
                if cur.get("channel_id") in (None, ch):
                    cur["channel_id"] = ch
                    cur[slot_msg] = mid
                else:
                    # cross-channel — fall back to the URL
                    cur[slot_link] = txt
            elif txt.startswith("http"):
                cur[slot_link] = txt
            else:
                await update.message.reply_text(
                    "❌ Not a valid link. Send a t.me/… link or a plain URL, or '-'.")
                return
        if step == "pdf_link":
            st["step"] = "dpp_link"
            await update.message.reply_text(
                "🧪 Step 4/4 — send the <b>DPP link</b>, or '-' to skip.",
                parse_mode=ParseMode.HTML, reply_markup=kb_nav)
            return
        # finalize
        row = db.execute_returning(
            """INSERT INTO lectures(chapter_id, name, channel_id, message_id,
                                    pdf_message_id, dpp_message_id,
                                    pdf_link, dpp_link)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (channel_id, message_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    pdf_message_id = COALESCE(EXCLUDED.pdf_message_id, lectures.pdf_message_id),
                    dpp_message_id = COALESCE(EXCLUDED.dpp_message_id, lectures.dpp_message_id),
                    pdf_link       = COALESCE(EXCLUDED.pdf_link,       lectures.pdf_link),
                    dpp_link       = COALESCE(EXCLUDED.dpp_link,       lectures.dpp_link)
               RETURNING lecture_id""",
            (d["chapter_id"], cur["name"], cur.get("channel_id"), cur.get("message_id"),
             cur.get("pdf_message_id"), cur.get("dpp_message_id"),
             cur.get("pdf_link"), cur.get("dpp_link")),
        )
        d["count"] += 1
        await update.message.reply_text(
            f"✅ Lecture #{row['lecture_id']} saved <b>({d['count']} total in this chapter)</b>.\n\n"
            f"Add another?",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb([
                [("➕ Next Lecture", "adm:linkadd:next")],
                [("✅ Done", "adm:linkadd:done")],
            ]))
        return




async def _step_check_rights(update, context, st, txt: str) -> None:
    try:
        ch_id = int(txt.strip())
    except ValueError:
        await update.message.reply_text("Channel ID must be a negative integer like -1001234567890.")
        return
    _reset(update.effective_user.id)
    status = await channel_scanner.check_channel_access(context.bot, ch_id)
    await update.message.reply_text(status, parse_mode=ParseMode.HTML,
                                    reply_markup=_home_kb())


# handle scan subject pick via callback
async def _on_scan_subject_pick(uid, q, subject_id: int) -> None:
    st = ADMIN_STATE.get(uid)
    if not st or st.get("action") != "scan":
        await _safe_edit(q, "Session lost.", _home_kb()); return
    d = st["data"]
    d["subject_id"] = subject_id
    resume = bool(d.get("resume"))
    await _safe_edit(q,
        f"🛰️ Scan started for <b>{d.get('batch_name','')}</b>. "
        f"You'll get progress updates here.",
        _home_kb())
    asyncio.create_task(
        channel_scanner.run_scan(
            q.get_bot(), q.from_user.id,
            d["batch_id"], d["subject_id"], d["channel_id"], resume=resume,
        )
    )
    _reset(uid)


# extend main callback router to handle scan_pick purpose
_orig_handle_pick = _handle_pick  # noqa
async def _handle_pick(uid, q, data: str) -> None:  # noqa: F811
    parts = data.split(":")
    purpose = parts[4] if len(parts) >= 5 else ""
    if purpose == "scan_pick":
        await _on_scan_subject_pick(uid, q, int(parts[3]))
        return
    await _orig_handle_pick(uid, q, data)


# ═════════════ optional shortcut command: /give_access ═════════════
async def give_access_cmd(update, context):
    uid = update.effective_user.id
    if not is_admin_user(uid) or not is_admin_session(uid):
        await update.message.reply_text("🔒 Send /admin first."); return
    if len(context.args) < 4:
        await update.message.reply_text(
            "Usage: /give_access telegram_id batch_id username password\n"
            "(Or use 💰 Payments → Approve in the admin menu.)"); return
    tg_id, bid, username, password = context.args[:4]
    b = db.query("SELECT * FROM batches WHERE batch_id=%s", (bid,), one=True)
    if not b:
        await update.message.reply_text("Batch not found."); return
    st = {"action": "give_access", "step": "password",
          "data": {"telegram_id": tg_id, "batch_id": bid,
                   "bname": b["name"], "username": username, "password": password}}
    await _finalize_give_access(update, context, st)
