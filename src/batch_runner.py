"""
batch_runner.py

Async batch runner with bounded concurrency.

IMPORTANT — current scope: the Classify/Diagnose/Decide steps here are a
DELIBERATE PLACEHOLDER (simple deterministic heuristics), not the real
LangGraph pipeline. Phases 3-4 replace `process_record`'s guts with an
actual `graph.ainvoke(state)` call once the specialist nodes exist. What
this file proves out RIGHT NOW is the concurrency + locking + persistence
+ Governor wiring end-to-end, so those pieces don't need to be re-tested
once the LLM nodes land on top.

Why bounded concurrency matters here specifically: the real cost in this
pipeline is I/O wait (LLM calls, simulated MCP/Razorpay API calls), not
CPU. Processing records sequentially would mean paying that latency N
times in a row. A semaphore-bounded task pool overlaps that I/O across
many records at once, while still capping how many are in-flight (so we
don't blow past a real API's rate limit once MCP nodes call out for real).
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from . import persistence
from .core_state import new_recovery_state, append_audit, RecoveryState
from .governor import validate_action
from .config import DEFAULT_POLICY, MerchantPolicy
from .mcp import mcp_razorpay, mcp_comm, mcp_ledger

MAX_CONCURRENT_RECORDS = 15
SAMPLE_DATA_PATH = Path(__file__).parent.parent / "data" / "synthetic_batch.json"
MERCHANT_TZ = ZoneInfo("Asia/Kolkata")  # quiet_hours / nudge-day-boundary are merchant-local, not UTC


# ---------------------------------------------------------------------------
# Placeholder heuristic "brain" — stands in for the LangGraph pipeline
# until Phase 3/4 nodes exist. Deterministic on purpose: this file's job
# right now is to prove the async/lock/persistence/Governor wiring works,
# not to reason about root causes.
# ---------------------------------------------------------------------------

def _propose_action(raw: dict[str, Any]) -> dict[str, Any]:
    loss_type = raw.get("loss_type")

    if loss_type == "payment_degradation":
        return {
            "action": "retry_payment",
            "transaction_id": raw["transaction_id"],
            "amount": raw["amount"],
            "decline_code": raw["decline_code"],
            "retry_attempt_number": raw.get("retry_count_so_far", 0) + 1,
        }
    if loss_type == "mandate_retry":
        return {
            "action": "retry_mandate",
            "mandate_id": raw["transaction_id"],
            "amount": raw["amount"],
            "retry_attempt_number": raw.get("retry_count_so_far", 0) + 1,
            "mandate_state": raw.get("mandate_state", "active"),
        }
    if loss_type == "receivable_overdue":
        return {
            "action": "escalate_to_human" if raw.get("dispute_flag") else "send_payment_link",
            "record_id": raw["record_id"],
            "reason": "disputed invoice" if raw.get("dispute_flag") else None,
            "transaction_id": raw.get("transaction_id", raw["record_id"]),
            "amount": raw["amount"],
            "channel": "email",
        } if not raw.get("dispute_flag") else {
            "action": "escalate_to_human",
            "record_id": raw["record_id"],
            "reason": "Invoice under dispute — never auto-chase.",
        }
    # checkout_dropoff, subscription_fail, promise_to_pay fall back to a nudge
    return {
        "action": "send_nudge",
        "record_id": raw["record_id"],
        "channel": raw.get("channel", "sms"),
        "language": "hinglish",
        "message": "Aapka payment complete nahi hua — yahan click karke complete karein.",
        "customer_phone": raw.get("customer_phone"),
    }


async def _dispatch_to_mcp(proposed: dict[str, Any]) -> dict[str, Any]:
    """
    Routes an approved action to the real MCP module functions. Returns
    the tool's raw result dict — the caller decides what counts as
    "recovered" money, since that's a business rule, not a transport concern.
    """
    action = proposed["action"]

    if action == "retry_payment":
        return await mcp_razorpay.execute_retry_payment(
            transaction_id=proposed["transaction_id"],
            amount=proposed["amount"],
            decline_code=proposed["decline_code"],
            retry_attempt_number=proposed["retry_attempt_number"],
        )
    if action == "send_payment_link":
        return await mcp_razorpay.execute_send_payment_link(
            transaction_id=proposed["transaction_id"],
            amount=proposed["amount"],
            channel=proposed["channel"],
            record_id=proposed.get("record_id"),
            customer_contact=proposed.get("customer_phone"),
            customer_name=proposed.get("customer_name"),
        )
    if action == "retry_mandate":
        return await mcp_razorpay.execute_retry_mandate(
            mandate_id=proposed["mandate_id"],
            amount=proposed["amount"],
            retry_attempt_number=proposed["retry_attempt_number"],
        )
    if action == "send_nudge":
        return await mcp_comm.execute_send_nudge(
            record_id=proposed["record_id"],
            channel=proposed["channel"],
            language=proposed["language"],
            message=proposed["message"],
            customer_phone=proposed.get("customer_phone"),
        )
    if action == "log_promise_to_pay":
        return await mcp_ledger.execute_log_promise_to_pay(
            record_id=proposed["record_id"],
            promised_date=proposed["promised_date"],
            amount=proposed["amount"],
        )
    if action == "escalate_to_human":
        return await mcp_ledger.execute_escalate_to_human(
            record_id=proposed["record_id"],
            reason=proposed.get("reason") or "Governor-forced or diagnosis-driven escalation.",
            priority=proposed.get("priority", "medium"),
        )
    if action == "close_case":
        return await mcp_ledger.execute_close_case(
            record_id=proposed["record_id"],
            reason=proposed.get("reason", "Resolved."),
        )

    raise ValueError(f"No MCP dispatch mapped for action '{action}'")


# Actions whose "success" means money is CONFIRMED recovered, not just attempted.
# send_payment_link is deliberately excluded: a dispatched link is a pending
# attempt, not a confirmed recovery, until something (out of scope for this
# batch) tells us the customer paid.
CONFIRMED_RECOVERY_ACTIONS = {"retry_payment", "retry_mandate"}


# ---------------------------------------------------------------------------
# Per-record pipeline
# ---------------------------------------------------------------------------

async def process_record(raw: dict[str, Any], semaphore: asyncio.Semaphore,
                          policy: MerchantPolicy = DEFAULT_POLICY) -> RecoveryState:
    async with semaphore:
        state = new_recovery_state(raw["record_id"], raw)
        state["loss_type"] = raw.get("loss_type")
        state["status"] = "classified"
        append_audit(state, "classify", f"Routed as loss_type={state['loss_type']} (deterministic, no LLM).")

        proposed = _propose_action(raw)
        state["proposed_action"] = proposed.get("action")
        state["proposed_action_params"] = proposed
        state["status"] = "diagnosed"

        context: dict[str, Any] = {
            "loss_type": raw.get("loss_type"),
            "amount": raw.get("amount", 0),
            "days_overdue": raw.get("days_overdue", 0),
            "cart_value": raw.get("cart_value", raw.get("amount", 0)),
            "mandate_category": raw.get("mandate_category"),
        }

        if proposed.get("action") == "send_nudge":
            since_midnight_ist = datetime.now(MERCHANT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            since_midnight_utc_iso = since_midnight_ist.astimezone(timezone.utc).isoformat()
            async with persistence.atomic_record_lock(raw["record_id"]):
                nudges_today = await persistence.count_nudges_today(raw["record_id"], since_midnight_utc_iso)
                context["nudges_sent_today"] = nudges_today
                context["nudges_sent_total"] = nudges_today
                context["current_hour"] = datetime.now(MERCHANT_TZ).hour  # merchant-local hour, not UTC

                decision = validate_action(proposed, context, policy)
                if decision.approved:
                    await persistence.record_nudge_sent(
                        raw["record_id"], proposed.get("channel", "sms"), datetime.now(timezone.utc).isoformat()
                    )
        else:
            decision = validate_action(proposed, context, policy)

        state["governor_approved"] = decision.approved
        state["governor_reasoning"] = decision.reasoning
        state["governor_blocked_reason"] = decision.blocked_reason
        state["status"] = "policy_checked"

        append_audit(
            state, "governor", decision.reasoning,
            action_taken=proposed.get("action") if decision.approved else None,
            escalated=decision.forced_escalation or proposed.get("action") == "escalate_to_human",
        )

        if decision.approved:
            result = await _dispatch_to_mcp(proposed)
            state["execution_result"] = result
            action = proposed["action"]
            tool_status = result.get("status")  # "success" | "failed" | "dispatched" | "delivery_failed" | "escalated" | "closed" | "proposed"

            confirmed_success = tool_status == "success" and action in CONFIRMED_RECOVERY_ACTIONS
            if confirmed_success:
                state["amount_recovered"] = state["amount_at_stake"]
                state["status"] = "recovered"
            elif action == "escalate_to_human":
                state["status"] = "escalated"
            elif action == "close_case":
                state["status"] = "closed"
            elif tool_status in {"failed", "delivery_failed"}:
                state["status"] = "unresolved"
            else:
                # dispatched (send_payment_link), send_nudge success, or proposed promise-to-pay:
                # attempted but not yet a confirmed outcome
                state["status"] = "executed"

            # mcp_ledger tools never write themselves (single-writer rule) —
            # the runner persists the side effects here, still inside whatever
            # lock the calling context holds for this record.
            if action == "escalate_to_human":
                pass  # status already reflected in the upsert_record call below
            elif action == "log_promise_to_pay" and tool_status == "proposed":
                await persistence.log_promise_to_pay(proposed["record_id"], proposed["promised_date"])

            append_audit(
                state, "execute", f"Dispatched {action} to MCP",
                action_taken=action, outcome=tool_status,
                escalated=(action == "escalate_to_human"),
            )
        else:
            state["status"] = "escalated" if decision.forced_escalation else "unresolved"
            append_audit(state, "execute", "Blocked by Governor — no execution.", outcome=decision.blocked_reason)

        await persistence.upsert_record(
            record_id=state["record_id"],
            transaction_id=state["transaction_id"],
            loss_type=state["loss_type"],
            status=state["status"],
            amount_at_stake=state["amount_at_stake"],
            amount_recovered=state["amount_recovered"],
            created_at=state["created_at"],
            updated_at=state["updated_at"],
        )
        await persistence.insert_audit_entries(state["record_id"], state["audit_trail"])

        return state


# ---------------------------------------------------------------------------
# Batch orchestration + metrics
# ---------------------------------------------------------------------------

async def run_batch(records: list[dict[str, Any]], max_concurrent: int = MAX_CONCURRENT_RECORDS) -> list[RecoveryState]:
    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [process_record(r, semaphore) for r in records]
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
        {"record_id": "r9", "transaction_id": "txn_9", "loss_type": "checkout_dropoff",
         "amount": 500, "cart_value": 500, "customer_phone": "+917798602488"},
        {"record_id": "r10", "transaction_id": "txn_10", "loss_type": "checkout_dropoff",
         "amount": 500, "cart_value": 500, "customer_phone": "+917798602488", "channel": "sms"},
        {"record_id": "r11", "transaction_id": "txn_11", "loss_type": "checkout_dropoff",
         "amount": 500, "cart_value": 500, "customer_phone": "+917798602488", "channel": "sms"},
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