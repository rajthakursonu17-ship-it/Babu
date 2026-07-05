"""Groq-powered smart caption parser for the channel scanner.

Given a caption / filename, returns:
    { "subject": str|None, "chapter": str|None, "lecture": str|None }
Falls back to regex heuristics if Groq unreachable.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from groq import Groq

from config import settings

logger = logging.getLogger(__name__)

_client: Optional[Groq] = None
if settings.GROQ_API_KEY:
    try:
        _client = Groq(api_key=settings.GROQ_API_KEY)
    except Exception as e:  # pragma: no cover
        logger.warning("Groq client init failed: %s", e)


_SYSTEM = (
    "You extract structured lecture metadata from a Telegram caption. "
    "Return STRICT JSON with keys: subject, chapter, lecture. "
    "Use null if a field is not clearly present. "
    "Subject = academic subject (Physics, Chemistry, Maths, Biology, English, etc). "
    "Chapter = chapter name/number. "
    "Lecture = full lecture title/name. Do not invent fields."
)


def _regex_fallback(text: str) -> dict:
    subj = chap = lec = None
    parts = re.split(r"[|\-–:]+", text)
    parts = [p.strip() for p in parts if p.strip()]
    for p in parts:
        low = p.lower()
        if not subj and any(k in low for k in
                            ("physics", "chemistry", "math", "biolog",
                             "english", "hindi", "reasoning", "science")):
            subj = p
        elif re.search(r"chapter|ch\.?\s*\d+|lesson", low):
            chap = p
        elif re.search(r"lecture|lec\.?\s*\d+|l-?\d+", low):
            lec = p
    if not lec and parts:
        lec = parts[-1]
    return {"subject": subj, "chapter": chap, "lecture": lec}


def parse_caption(caption: str) -> dict:
    text = (caption or "").strip()
    if not text:
        return {"subject": None, "chapter": None, "lecture": None}

    if _client is None:
        return _regex_fallback(text)

    try:
        resp = _client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text[:2000]},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        return {
            "subject": data.get("subject") or None,
            "chapter": data.get("chapter") or None,
            "lecture": data.get("lecture") or None,
        }
    except Exception as e:
        logger.warning("Groq parse failed (%s); falling back to regex", e)
        return _regex_fallback(text)
