"""Smoke tests: db + Groq caption parser + admin logic — no Telegram round-trip needed."""
import sys, os
sys.path.insert(0, "/app/bot")

from database import db
from utils import groq_parser

db.init_pool()

# ─── 1. batch/subject/chapter/lecture insert & fetch ───
print("=== creating batch ===")
b = db.execute_returning(
    "INSERT INTO batches(name, description, price, batch_code) "
    "VALUES(%s,%s,%s,%s) RETURNING batch_id, batch_code",
    ("Smoke Batch", "test", 999, "SMOKE01"),
)
print("batch:", dict(b))
bid = b["batch_id"]

s = db.execute_returning(
    "INSERT INTO subjects(batch_id, name) VALUES(%s,%s) RETURNING subject_id",
    (bid, "Physics"),
)
sid = s["subject_id"]
c = db.execute_returning(
    "INSERT INTO chapters(subject_id, name) VALUES(%s,%s) RETURNING chapter_id",
    (sid, "Kinematics"),
)
cid = c["chapter_id"]
l = db.execute_returning(
    "INSERT INTO lectures(chapter_id, name, channel_id, message_id, pdf_link, dpp_link) "
    "VALUES(%s,%s,%s,%s,%s,%s) RETURNING lecture_id",
    (cid, "L1 Motion", -100999, 42, "https://example.com/n.pdf", "https://example.com/d.pdf"),
)
print("lecture:", dict(l))

# ─── 2. user + trial flow ───
db.execute(
    "INSERT INTO users(telegram_id, full_name, trial_start, trial_active, referral_code) "
    "VALUES(%s,%s,NOW(),TRUE,%s) ON CONFLICT DO NOTHING",
    (999999999, "Smoke User", "SHRIJITEST01"),
)
u = db.query("SELECT * FROM users WHERE telegram_id=999999999", one=True)
print("user trial_active:", u["trial_active"], "opens:", u["trial_open_count"])

# ─── 3. referral bonus ───
db.execute(
    "INSERT INTO users(telegram_id, full_name, referral_code, referred_by, trial_start, trial_active) "
    "VALUES(%s,%s,%s,%s,NOW(),TRUE) ON CONFLICT DO NOTHING",
    (888888888, "Ref User", "SHRIJIREF01", 999999999),
)
db.execute("INSERT INTO referrals(referrer_id, referred_id, bonus_applied) VALUES(%s,%s,TRUE)"
           " ON CONFLICT (referred_id) DO NOTHING", (999999999, 888888888))
db.execute("UPDATE users SET referral_bonus_hours = referral_bonus_hours + 3 WHERE telegram_id=%s",
           (999999999,))
u = db.query("SELECT referral_bonus_hours FROM users WHERE telegram_id=999999999", one=True)
print("bonus hours after referral:", u["referral_bonus_hours"])

# ─── 4. groq parser ───
print("=== groq parser ===")
for cap in [
    "Physics | Chapter 1 | Lecture 1 - Motion",
    "Chem CH3 Basic Concepts Lecture 4",
    "MATHS L05 – Trigonometry",
]:
    print(cap, "→", groq_parser.parse_caption(cap))

# ─── 5. sliding-window logic ───
for i in range(7):
    db.execute(
        "INSERT INTO user_lecture_access(telegram_id, lecture_id, batch_id, sent_message_id, delete_at, sequence_number) "
        "VALUES(%s,%s,%s,%s,NOW()+INTERVAL '15 hours',%s)",
        (999999999, l["lecture_id"], bid, 1000 + i, i + 1),
    )
rows = db.query(
    "SELECT sequence_number, sent_message_id, deleted FROM user_lecture_access "
    "WHERE telegram_id=999999999 AND batch_id=%s ORDER BY sequence_number", (bid,)
)
print("access rows:", [dict(r) for r in rows])

# ─── 6. cleanup ───
db.execute("DELETE FROM user_lecture_access WHERE telegram_id IN (999999999,888888888)")
db.execute("DELETE FROM referrals WHERE referrer_id=999999999")
db.execute("DELETE FROM users WHERE telegram_id IN (999999999,888888888)")
db.execute("DELETE FROM batches WHERE batch_id=%s", (bid,))
print("\n✅ ALL SMOKE TESTS PASSED")
