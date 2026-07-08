import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).parent.parent
load_dotenv(ROOT_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def _ids(name: str) -> list[int]:
    raw = os.environ.get(name, "") or ""
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except ValueError:
            pass
    return out


BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

ADMIN_IDS = set(_ids("ADMIN_IDS"))
CHANNEL_IDS = _ids("CHANNEL_IDS")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
ADMIN_CONTACT = os.environ.get("ADMIN_CONTACT", "@Rajput4444")

FREE_TRIAL_HOURS = _int("FREE_TRIAL_HOURS", 24)
FREE_TRIAL_OPEN_LIMIT = _int("FREE_TRIAL_OPEN_LIMIT", 50)
PAID_OPEN_LIMIT = _int("PAID_OPEN_LIMIT", 100)
LECTURE_DELETE_AFTER_HOURS = _int("LECTURE_DELETE_AFTER_HOURS", 15)
SLIDING_WINDOW_SIZE = _int("SLIDING_WINDOW_SIZE", 5)
REFER_BONUS_HOURS = _int("REFER_BONUS_HOURS", 3)
