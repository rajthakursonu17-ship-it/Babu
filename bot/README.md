# 🎓 Shriji Institute — Telegram Education Bot

Production-ready Telegram bot that delivers structured lecture content
(Batches → Subjects → Chapters → Lectures) with:

- 🎁 24-hour free trial (50 opens) + paid batches (100 opens each)
- 🔗 Referral system (+3 bonus hours per friend, stackable)
- 🎥 Forward-protected videos, 15-hour auto-delete, sliding-5 cleanup
- 🛰️ Smart channel scanner powered by **Groq LLM** (parses captions into
  Subject/Chapter/Lecture automatically), resumable, exhaustive
- ⚙️ Password-protected admin panel with full CRUD, broadcast, settings, and
  live payment approval flow
- 🗄️ PostgreSQL (Supabase) — 9 tables per spec

## 🚀 Tech
- Python 3.11 • python-telegram-bot 20.7
- psycopg2 + Supabase Postgres
- Groq (llama-3.3-70b-versatile) for caption parsing
- APScheduler (via PTB job-queue) for auto-delete sweeper

## ⚙️ Env Variables
Copy `.env.example` → `.env` and fill:

```
BOT_TOKEN=<from @BotFather>
CHANNEL_IDS=<-1001234567890,-1009876543210>   # source channels, comma-sep
ADMIN_IDS=<your telegram numeric id>
ADMIN_PASSWORD=<pick one>
DATABASE_URL=<Supabase pooler URI>
GROQ_API_KEY=<from console.groq.com>
GROQ_MODEL=llama-3.3-70b-versatile
FREE_TRIAL_HOURS=24
FREE_TRIAL_OPEN_LIMIT=50
PAID_OPEN_LIMIT=100
LECTURE_DELETE_AFTER_HOURS=15
SLIDING_WINDOW_SIZE=5
REFER_BONUS_HOURS=3
```

## ▶️ Run locally
```
pip install -r requirements.txt
python bot.py
```
The bot applies the schema idempotently on first run.

## ☁️ Deploy on Railway
1. Push this folder to GitHub
2. In Railway → **New Project → Deploy from GitHub**
3. Add all env variables from `.env`
4. Railway auto-detects `Procfile` / `railway.json`
5. Deploy — polling starts automatically

## 👤 User commands
| Command       | What it does                            |
|---------------|-----------------------------------------|
| `/start`      | Register + activate free trial          |
| `/refer`      | Referral link + bonus stats             |
| `/myaccount`  | Trial/paid usage summary                |
| `/support`    | Ping admin                              |

## 🛠 Admin commands (after `/admin` + password)
| Command | Purpose |
|---|---|
| `/add_batch Name|Desc|Price` | Create batch |
| `/edit_batch id|field|value` | Edit batch |
| `/del_batch id` | Delete |
| `/add_subject batch_id|Name` | ... |
| `/add_chapter subject_id|Name` | ... |
| `/add_lecture chapter_id|Name|channel_id|message_id|pdf|dpp` | ... |
| `/bulk_add chapter_id` → send multi-line paste | Fast bulk |
| `/scan batch_code channel_id subject_id` | Full historical scan |
| `/update_channel batch_code channel_id subject_id` | Delta scan |
| `/give_access tg_id batch_id username password` | Approve purchase |
| `/list_users`, `/search_user q`, `/user_info id` | User mgmt |
| `/broadcast Msg` | Fan-out |
| `/set_setting KEY VALUE` | Live config |
| `/set_admin_password newpw` | Rotate password |

## 🔐 Content protection rules
- Videos are **copied** (not forwarded raw) with `protect_content=True`, so
  users cannot re-forward or save them.
- Every user chat keeps only the last 5 opened videos (sliding cleanup).
- Independently, every video is deleted 15 h after delivery by the sweeper.
- PDFs / DPPs are direct links, freely shareable per spec.

## 📁 Structure
See `/app/bot/` — matches the spec.
