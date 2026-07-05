"""Thin sync psycopg2 pool + helpers.  Bot is single-process, so this is safe."""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import psycopg2
import psycopg2.extras
from psycopg2 import pool

from config import settings

logger = logging.getLogger(__name__)

_POOL: pool.SimpleConnectionPool | None = None
_LOCK = threading.Lock()


def init_pool(minconn: int = 1, maxconn: int = 8) -> None:
    global _POOL
    with _LOCK:
        if _POOL is None:
            _POOL = pool.SimpleConnectionPool(
                minconn, maxconn, dsn=settings.DATABASE_URL
            )
            logger.info("PG pool initialised")


@contextmanager
def get_conn():
    assert _POOL is not None, "Pool not initialised"
    conn = _POOL.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _POOL.putconn(conn)


def query(sql: str, params: Iterable | None = None, one: bool = False) -> Any:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            if cur.description is None:
                return None
            return cur.fetchone() if one else cur.fetchall()


def execute(sql: str, params: Iterable | None = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())


def execute_returning(sql: str, params: Iterable | None = None):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()


def apply_schema() -> None:
    schema = (Path(__file__).parent / "schema.sql").read_text()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(schema)
    logger.info("Schema applied")
