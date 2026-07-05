# Shriji Institute — Telegram Education Bot (PRD)

## Original problem statement
Production-ready Telegram bot for Shriji Institute delivering
Batches → Subjects → Chapters → Lectures (video + PDF + DPP), with
free trial + paid batches, referrals, content protection, admin panel,
and a smart Groq-powered channel scanner.

## Tech stack
- Python 3.11, python-telegram-bot 20.7 (polling + job-queue/APScheduler)
- PostgreSQL via Supabase (pooler URI, region ap-northeast-1)
- Groq LLM `llama-3.3-70b-versatile` for caption parsing (fallback regex)
- bcrypt for user credential hashing
- Deploy target: Railway (Procfile + railway.json included)

## User personas
- **Student**: browses batches, uses 24h free trial (50 opens),
  purchases batches for 100 opens each, earns +3 bonus hours per referral.
- **Admin (owner)**: manages content, approves purchases, runs channel scans,
  broadcasts, tweaks live settings.

## Core requirements (static)
1. Batch → Subject → Chapter → Lecture hierarchy
2. Free trial: 24 h + 50 opens (configurable)
3. Paid: 100 opens per batch (configurable)
4. Referral: +3 bonus hours per new user, stackable
5. Videos: forward-protected + 15h auto-delete + sliding-5 cleanup
6. PDF/DPP: freely shareable direct links
7. Admin password-gated, re-verified each session
8. Exhaustive resumable channel scan (Groq caption parsing)
9. Manual payment approval via `/give_access`, notifies user with hashed creds
10. Broadcast, user search, live setting changes

## Implemented (2026-01-05)
- ✅ Full 9-table Postgres schema, applied to Supabase
- ✅ /start with trial activation + referral capture from deep-link
- ✅ Browse UI (batches → subjects → chapters → lectures) with decorative HTML formatting
- ✅ Lecture delivery: forward-protected `copy_message`, sliding-5 cleanup, 15h sweep job
- ✅ Trial/paid limit enforcement per user + per batch
- ✅ `/refer`, referral link, bonus stacking
- ✅ Buy flow: notifies admin(s), creates pending_payment row
- ✅ Admin panel: `/admin` password gated + inline menu
- ✅ Batch/Subject/Chapter/Lecture CRUD commands
- ✅ Bulk-add lectures via multi-line paste
- ✅ Smart `/scan` + `/update_channel` (resumable, Groq-classified, per-50 progress messages)
- ✅ `/give_access` with bcrypt-hashed credentials sent to user
- ✅ Broadcast, list/search/info users, live `/set_setting`, `/set_admin_password`
- ✅ APScheduler sweeper (every 5 min) auto-deletes expired videos
- ✅ Groq LLM classifier + regex fallback (tested on 3 caption styles — all parsed)
- ✅ Railway deploy files + full README

## Live services
- `shriji_bot` under supervisor (`/etc/supervisor/conf.d/supervisord_shriji_bot.conf`)
- Handle: **@Babujiihebot**

## Backlog / P1
- Automatic payment gateway (Razorpay/UPI intent) — file structure keeps `payment_handlers.py` reserved
- Per-user natural language search over lectures (Groq embeddings or keyword)
- CSV export of users / broadcasts
- Rich media broadcast (photo/video with caption)

## Next tasks
1. User to add bot as **admin** in the source channel(s) and run `/scan BATCH_CODE CHANNEL_ID SUBJECT_ID` for first import.
2. User to test the full journey on Telegram: /start → browse → buy → /admin → /give_access.
3. Push repo to GitHub → deploy on Railway with the same env vars.
