"""
batch_runner.py

Async batch runner with bounded concurrency.

Phase 3 update: process_record now builds the initial RecoveryState and
hands it to the compiled LangGraph DAG (graph/state_graph.py) via
ainvoke() — it no longer contains the pipeline logic itself. Classify,
diagnose (still placeholder — see graph/nodes.py), governor, and execute
all live in graph/nodes.py now. This file's job is purely: concurrency,
persistence, and metrics — the same separation of concerns any LangGraph
consumer would have.

Why bounded concurrency still matters here specifically: the real cost
in this pipeline is I/O wait (LLM calls once Phase 4 lands, real MCP/
Razorpay/Twilio calls today). A semaphore-bounded task pool overlaps
that I/O across many records at once, while capping how many are
in-flight so we don't blow past a real API's rate limit.
"""
from __future__ import annotations
import asyncio
import json
import time
from pathlib import Path
from typing import Any, cast

from . import persistence
from .core_state import new_recovery_state, RecoveryState
from .config import DEFAULT_POLICY, MerchantPolicy
from .graph.state_graph import build_graph

MAX_CONCURRENT_RECORDS = 15
SAMPLE_DATA_PATH = Path(__file__).parent.parent / "data" / "synthetic_batch.json"


async def process_record(raw: dict[str, Any], semaphore: asyncio.Semaphore, graph: Any) -> RecoveryState:
    async with semaphore:
        state = new_recovery_state(raw["record_id"], raw)
        final_state = cast(RecoveryState, await graph.ainvoke(state))

        await persistence.upsert_record(
            record_id=final_state["record_id"],
            transaction_id=final_state["transaction_id"],
            loss_type=final_state["loss_type"],
            status=final_state["status"],
            amount_at_stake=final_state["amount_at_stake"],
            amount_recovered=final_state["amount_recovered"],
            created_at=final_state["created_at"],
            updated_at=final_state["updated_at"],
        )
        await persistence.insert_audit_entries(final_state["record_id"], final_state["audit_trail"])

        return final_state


# ---------------------------------------------------------------------------
# Batch orchestration + metrics
# ---------------------------------------------------------------------------

async def run_batch(records: list[dict[str, Any]], max_concurrent: int = MAX_CONCURRENT_RECORDS,
                     policy: MerchantPolicy = DEFAULT_POLICY) -> list[RecoveryState]:
    semaphore = asyncio.Semaphore(max_concurrent)
    graph = build_graph(policy)
    tasks = [process_record(r, semaphore, graph) for r in records]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    states: list[RecoveryState] = []
    exceptions: list[dict[str, Any]] = []
    for r, res in zip(records, results):
        if isinstance(res, Exception):
            error_entry: dict[str, Any] = {"record_id": r.get("record_id"), "error": str(res)}
            exceptions.append(error_entry)
        else:
            states.append(cast(RecoveryState, res))
    if exceptions:
        print(f"WARNING: {len(exceptions)} records raised exceptions during processing: {exceptions}")
    return states


def compute_metrics(states: list[RecoveryState]) -> dict[str, Any]:
    total_detected = sum(s["amount_at_stake"] for s in states)
    total_recovered = sum(s["amount_recovered"] for s in states)
    by_category: dict[str, dict[str, float]] = {}
    escalations = 0
    exceptions: list[dict[str, Any]] = []

    for s in states:
        cat = s["loss_type"] or "unknown"
        by_category.setdefault(cat, {"detected": 0.0, "recovered": 0.0, "count": 0})
        by_category[cat]["detected"] += s["amount_at_stake"]
        by_category[cat]["recovered"] += s["amount_recovered"]
        by_category[cat]["count"] += 1
        if s["status"] == "escalated":
            escalations += 1
        if s["status"] == "unresolved":
            reason = s["governor_blocked_reason"]
            if reason is None and s["execution_result"]:
                reason = s["execution_result"].get("failure_reason") or f"execution status: {s['execution_result'].get('status')}"
            exc_entry: dict[str, Any] = {
                "record_id": s["record_id"],
                "loss_type": s["loss_type"],
                "reason": reason,
            }
            exceptions.append(exc_entry)

    for cat, vals in by_category.items():
        vals["recovery_rate"] = (vals["recovered"] / vals["detected"]) if vals["detected"] else 0.0

    return {
        "total_records": len(states),
        "total_detected_inr": round(total_detected, 2),
        "total_recovered_inr": round(total_recovered, 2),
        "overall_recovery_rate": round(total_recovered / total_detected, 4) if total_detected else 0.0,
        "escalation_count": escalations,
        "escalation_rate": round(escalations / len(states), 4) if states else 0.0,
        "by_category": by_category,
        "exceptions": exceptions,
    }


def _load_records() -> list[dict[str, Any]]:
    if SAMPLE_DATA_PATH.exists():
        with open(SAMPLE_DATA_PATH) as f:
            return json.load(f)
    # Minimal embedded sample so this file is runnable before Phase 0's
    # generator script exists. Replace by pointing SAMPLE_DATA_PATH at the
    # real 150-300 record synthetic batch once it's generated.
    return [
        {"record_id": "r1", "transaction_id": "txn_1", "loss_type": "payment_degradation",
         "amount": 1200, "decline_code": "insufficient_funds", "retry_count_so_far": 0},
        {"record_id": "r2", "transaction_id": "txn_2", "loss_type": "payment_degradation",
         "amount": 3000, "decline_code": "payment_risk_check_failed", "retry_count_so_far": 0},
        {"record_id": "r3", "transaction_id": "txn_3", "loss_type": "mandate_retry",
         "amount": 999, "mandate_state": "active", "retry_count_so_far": 4},
        {"record_id": "r4", "transaction_id": "txn_4", "loss_type": "receivable_overdue",
         "amount": 75000, "days_overdue": 60, "dispute_flag": False},
        {"record_id": "r5", "transaction_id": "txn_5", "loss_type": "checkout_dropoff",
         "amount": 450, "cart_value": 450},
    ]


async def main() -> None:
    persistence.init_db()
    records = _load_records()
    print(f"Loaded {len(records)} records. Running batch with max_concurrent={MAX_CONCURRENT_RECORDS}...")

    start = time.perf_counter()
    states = await run_batch(records)
    elapsed = time.perf_counter() - start

    metrics = compute_metrics(states)
    print(f"\nBatch completed in {elapsed:.2f}s")
    print(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    asyncio.run(main())