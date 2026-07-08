"""Keyboard & message formatters."""
from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows]
    )


def main_menu_kb() -> InlineKeyboardMarkup:
    return kb([
        [("📚 Browse Batches", "batches"), ("⭐ My Access", "myaccess")],
        [("🔗 Refer & Earn", "refer"), ("💳 Buy Batch", "buy_menu")],
        [("👤 My Profile", "profile"), ("ℹ️ Help", "help")],
    ])


def batches_kb(batches) -> InlineKeyboardMarkup:
    rows = [[(f"📚 {b['name']}", f"batch:{b['batch_id']}")] for b in batches]
    rows.append([("🏠 Home", "home")])
    return kb(rows)


def batch_detail_kb(batch_id: int, is_purchased: bool) -> InlineKeyboardMarkup:
    rows = [[("📖 View Subjects", f"subjects:{batch_id}")]]
    if not is_purchased:
        rows.append([("💳 Buy Now", f"buy:{batch_id}")])
    rows.append([("⬅️ Batches", "batches"), ("🏠 Home", "home")])
    return kb(rows)


def subjects_kb(batch_id: int, subjects) -> InlineKeyboardMarkup:
    rows = [[(f"📖 {s['name']}", f"subject:{s['subject_id']}")] for s in subjects]
    rows.append([("⬅️ Back", f"batch:{batch_id}"), ("🏠 Home", "home")])
    return kb(rows)


def chapters_kb(subject_id: int, batch_id: int, chapters) -> InlineKeyboardMarkup:
    rows = [[(f"📝 {c['name']}", f"chapter:{c['chapter_id']}")] for c in chapters]
    rows.append([("⬅️ Subjects", f"subjects:{batch_id}"), ("🏠 Home", "home")])
    return kb(rows)


def lectures_kb(chapter_id: int, subject_id: int, lectures) -> InlineKeyboardMarkup:
    rows = [[(f"🎥 {l['name']}", f"lecture:{l['lecture_id']}")] for l in lectures]
    rows.append([("⬅️ Chapters", f"subject:{subject_id}"), ("🏠 Home", "home")])
    return kb(rows)


def lecture_actions_kb(lecture: dict, chapter_id: int) -> InlineKeyboardMarkup:
    rows = [[("🎥 Watch Video", f"watch:{lecture['lecture_id']}")]]
    extras = []
    if lecture.get("pdf_link") or lecture.get("pdf_message_id") or lecture.get("pdf_file_id"):
        extras.append(("📄 Notes PDF", f"pdf:{lecture['lecture_id']}"))
    if lecture.get("dpp_link") or lecture.get("dpp_message_id") or lecture.get("dpp_file_id"):
        extras.append(("🧪 DPP", f"dpp:{lecture['lecture_id']}"))
    if extras:
        rows.append(extras)
    rows.append([("⬅️ Back", f"chapter:{chapter_id}"), ("🏠 Home", "home")])
    return kb(rows)


def welcome_text(name: str, trial_hours: int, trial_limit: int) -> str:
    return (
        f"╔══════════════════════════╗\n"
        f"   🎓 <b>SHRIJI INSTITUTE</b> 🎓\n"
        f"╚══════════════════════════╝\n\n"
        f"Hello <b>{name}</b> 👋\n"
        f"Welcome to your personal learning companion!\n\n"
        f"🎁 <b>FREE TRIAL ACTIVATED</b>\n"
        f"⏳ Duration: <b>{trial_hours} hours</b>\n"
        f"🎬 Lectures/PDFs: <b>{trial_limit} opens</b>\n\n"
        f"📚 Tap below to start exploring batches — everything is one tap away.\n"
        f"🔗 Invite friends with <code>/refer</code> and earn "
        f"<b>+3 bonus hours</b> per friend!"
    )


def admin_menu_kb() -> InlineKeyboardMarkup:
    return kb([
        [("📚 Batches", "adm:batches"), ("📖 Subjects", "adm:subjects")],
        [("📝 Chapters", "adm:chapters"), ("🎥 Lectures", "adm:lectures")],
        [("🛰️ Channel Scan", "adm:scan"), ("👥 Users", "adm:users")],
        [("📣 Broadcast", "adm:broadcast"), ("⚙️ Settings", "adm:settings")],
        [("💰 Payments", "adm:payments"), ("🚪 Exit", "adm:exit")],
    ])
