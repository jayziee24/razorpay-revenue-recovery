"""
persistence.py

Lightweight SQLite-backed persistence used by the batch runner and
webhook receiver. This file provides minimal, well-typed APIs the
runner expects: per-record locks (in-memory), simple tables for
recovery records, nudges, promise-to-pay, and audit entries, and a few
helpers used by tests and the webhook flow.

Implementation notes:
- Uses builtin `sqlite3` in `asyncio.to_thread` to avoid additional
  runtime dependencies.
- Keeps per-record asyncio locks in-memory to provide atomic updates
  in the runner process. This is sufficient for the demo harness.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Any, Sequence
import asyncio

DB_PATH = Path(__file__).parent.parent / "recovery.db"

# In-process per-record locks to emulate a single-writer rule.
_record_locks: dict[str, asyncio.Lock] = {}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS recovery_records (
            record_id TEXT PRIMARY KEY,
            transaction_id TEXT,
            loss_type TEXT,
            status TEXT,
            amount_at_stake REAL,
            amount_recovered REAL,
            created_at TEXT,
            updated_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT,
            timestamp TEXT,
            node TEXT,
            reasoning TEXT,
            action_taken TEXT,
            outcome TEXT,
            escalated INTEGER
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS nudges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT,
            channel TEXT,
            sent_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS promise_to_pay (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_id TEXT,
            promised_date TEXT,
            amount REAL,
            logged_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


@asynccontextmanager
async def atomic_record_lock(record_id: str) -> AsyncGenerator[None, None]:
    """Async context manager that acquires an in-memory lock for a record id."""
    lock = _record_locks.setdefault(record_id, asyncio.Lock())
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


async def count_nudges_today(record_id: str, since_midnight_utc_iso: str) -> int:
    def _count() -> int:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(1) as c FROM nudges WHERE record_id = ? AND sent_at >= ?", (record_id, since_midnight_utc_iso))
        row = cur.fetchone()
        conn.close()
        return int(row[0]) if row else 0

    return await asyncio.to_thread(_count)


async def record_nudge_sent(record_id: str, channel: str, sent_at_utc_iso: str) -> None:
    def _insert() -> None:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute("INSERT INTO nudges (record_id, channel, sent_at) VALUES (?, ?, ?)", (record_id, channel, sent_at_utc_iso))
        conn.commit()
        conn.close()

    await asyncio.to_thread(_insert)


async def log_promise_to_pay(record_id: str, promised_date: str, amount: float | None = None) -> None:
    def _insert() -> None:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO promise_to_pay (record_id, promised_date, amount, logged_at) VALUES (?, ?, ?, ?)",
            (record_id, promised_date, amount or 0.0, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()

    await asyncio.to_thread(_insert)


async def upsert_record(record_id: str, transaction_id: str | None, loss_type: str | None,
                        status: str, amount_at_stake: float, amount_recovered: float,
                        created_at: str, updated_at: str) -> None:
    def _upsert() -> None:
        conn = _get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO recovery_records (record_id, transaction_id, loss_type, status, amount_at_stake, amount_recovered, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            "ON CONFLICT(record_id) DO UPDATE SET transaction_id=excluded.transaction_id, loss_type=excluded.loss_type, status=excluded.status, amount_at_stake=excluded.amount_at_stake, amount_recovered=excluded.amount_recovered, created_at=excluded.created_at, updated_at=excluded.updated_at",
            (record_id, transaction_id, loss_type, status, amount_at_stake, amount_recovered, created_at, updated_at),
        )
        conn.commit()
        conn.close()

    await asyncio.to_thread(_upsert)


async def insert_audit_entries(record_id: str, audit_trail: Sequence[Any]) -> None:
    def _insert_all() -> None:
        conn = _get_conn()
        cur = conn.cursor()
        for entry in audit_trail:
            cur.execute(
                "INSERT INTO audit_entries (record_id, timestamp, node, reasoning, action_taken, outcome, escalated) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id,
                    entry.get("timestamp"),
                    entry.get("node"),
                    entry.get("reasoning"),
                    entry.get("action_taken"),
                    entry.get("outcome"),
                    1 if entry.get("escalated") else 0,
                ),
            )
        conn.commit()
        conn.close()

    await asyncio.to_thread(_insert_all)


async def mark_recovered_by_reference(record_id: str, amount_recovered: float) -> None:
    def _mark() -> None:
        conn = _get_conn()
        cur = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cur.execute("UPDATE recovery_records SET amount_recovered = ?, status = ?, updated_at = ? WHERE record_id = ?",
                    (amount_recovered, "recovered", now, record_id))
        conn.commit()
        conn.close()

    await asyncio.to_thread(_mark)
