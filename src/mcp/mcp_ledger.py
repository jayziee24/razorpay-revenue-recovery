"""
mcp_ledger.py

Simulated MCP boundary for local evaluation (see mcp_razorpay.py docstring
for the full rationale).

IMPORTANT — this file was corrected after an earlier draft had
execute_log_promise_to_pay calling persistence.log_promise_to_pay()
directly. That violated the single-writer rule: the runner/graph is the
ONLY thing that writes to SQLite. Every function here now returns data
only. The runner is responsible for calling the matching persistence.*
function after inspecting the tool's return value.

Why this rule matters concretely: if both a tool and the runner can write
recovery_records (or promise_to_pay) independently, you get two paths
racing to mutate the same row. That's exactly the split-brain failure
mode the atomic_record_lock in persistence.py was built to prevent
elsewhere — letting a tool bypass it here would quietly reopen that hole.
"""

from __future__ import annotations

from typing import Any
import asyncio


async def execute_log_promise_to_pay(record_id: str, promised_date: str, amount: float) -> dict[str, Any]:
    """
    Returns the proposed promise-to-pay record. Does NOT write to the
    database. Caller (runner) must call persistence.log_promise_to_pay()
    after receiving this, inside the same atomic_record_lock it already
    holds for this record.
    """
    await asyncio.sleep(0.01)
    return {
        "status": "proposed",
        "action": "log_promise_to_pay",
        "record_id": record_id,
        "promised_date": promised_date,
        "amount": amount,
        "note": "not yet persisted — runner must write this via persistence.log_promise_to_pay",
    }


async def execute_escalate_to_human(record_id: str, reason: str, priority: str = "medium") -> dict[str, Any]:
    await asyncio.sleep(0.01)
    return {
        "status": "escalated",
        "action": "escalate_to_human",
        "record_id": record_id,
        "reason": reason,
        "priority": priority,
        "assigned_queue": "ops_manual_review",
        "note": "runner must persist status='escalated' via persistence.upsert_record",
    }


async def execute_close_case(record_id: str, reason: str) -> dict[str, Any]:
    await asyncio.sleep(0.01)
    return {
        "status": "closed",
        "action": "close_case",
        "record_id": record_id,
        "reason": reason,
        "note": "runner must persist status='closed' via persistence.upsert_record",
    }