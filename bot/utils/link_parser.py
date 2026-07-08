"""Parse Telegram message links → (channel_id, message_id).

Supports:
  https://t.me/c/1234567890/45           (private channel)
  https://t.me/channelusername/45        (public channel)
  https://telegram.me/... variants
  http:// variants
Returns (None, None) if the link isn't parseable.
"""
from __future__ import annotations

import re
from typing import Optional

_PRIVATE = re.compile(
    r"https?://(?:t|telegram)\.me/c/(\d+)/(\d+)(?:/\d+)?",
    re.IGNORECASE,
)
_PUBLIC = re.compile(
    r"https?://(?:t|telegram)\.me/([A-Za-z]\w{3,})/(\d+)(?:/\d+)?",
    re.IGNORECASE,
)


def parse_message_link(link: str) -> tuple[Optional[int | str], Optional[int]]:
    """Return (channel_id_or_username, message_id) or (None, None)."""
    link = (link or "").strip()
    m = _PRIVATE.search(link)
    if m:
        raw = int(m.group(1))
        # Bot API channel IDs are -100 + the numeric id shown in `c/`
        channel_id = int(f"-100{raw}")
        return channel_id, int(m.group(2))
    m = _PUBLIC.search(link)
    if m:
        return "@" + m.group(1), int(m.group(2))
    return None, None
