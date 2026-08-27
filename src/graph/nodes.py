"""
nodes.py

Each function here is one LangGraph node, operating on RecoveryState.
Every node returns the FULL state object (not a partial update) — that
means no LangGraph reducers are needed for merging; each node's return
value simply replaces the running state wholesale, including the
already-mutated audit_trail list.

Phase 4 update: diagnose_node now calls real Gemini reasoning first,
falling back to the old deterministic heuristic (_heuristic_propose_action)
only if the LLM call fails for any reason. Every diagnosis records which
path produced it (real_gemini_diagnosis vs fallback_heuristic) in the
audit trail, so this is checkable after the fact, not just claimed.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..core_state import RecoveryState, append_audit
from ..router import classify_loss_type
from ..governor import validate_action
from ..config import MerchantPolicy
from .. import persistence
from ..mcp import mcp_razorpay, mcp_comm, mcp_ledger
from ..integrations import gemini_client
from ..taxonomy import CARD_DECLINE_CODES, UPI_DECLINE_CODES

MERCHANT_TZ = ZoneInfo("Asia/Kolkata")

CONFIRMED_RECOVERY_ACTIONS = {"retry_payment", "retry_mandate"}


async def classify_node(state: RecoveryState) -> RecoveryState:
    state["iteration_count"] += 1
    loss_type, reasoning = classify_loss_type(state["raw_data"])
    state["loss_type"] = loss_type
    state["status"] = "classified"
    append_audit(state, "classify", reasoning)
    return state


def iteration_guard(state: RecoveryState) -> str:
    """Conditional edge after classify. Currently nothing loops back to
    classify in this graph, so this guard is unreachable in practice —
    it exists as required infrastructure for Phase 4, where a diagnosis
    step (e.g. detecting mandate drift) may need to re-route through
    classify/diagnose again, bounded by max_iterations."""
    if state["iteration_count"] > state["max_iterations"]:
        state["status"] = "unresolved"
        append_audit(state, "iteration_guard", "max_iterations exceeded — halting.", escalated=True)
        return "halt"
    if state["loss_type"] is None:
        state["status"] = "escalated"
        append_audit(state, "iteration_guard", "Unclassifiable record — routing to escalation.", escalated=True)
        return "halt"
    return "continue"


# ---------------------------------------------------------------------------
# Real diagnosis via Gemini, with deterministic fallback on any failure
# ---------------------------------------------------------------------------

def _heuristic_propose_action(raw: dict[str, Any], loss_type: Optional[str]) -> dict[str, Any]:
    """The original placeholder logic — now used ONLY as a fallback when
    the real Gemini call fails for any reason (missing key, network,
    malformed response)."""
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
        if raw.get("dispute_flag"):
            return {
                "action": "escalate_to_human",
                "record_id": raw["record_id"],
                "reason": "Invoice under dispute — never auto-chase.",
            }
        return {
            "action": "send_payment_link",
            "record_id": raw["record_id"],
            "transaction_id": raw.get("transaction_id", raw["record_id"]),
            "amount": raw["amount"],
            "channel": "email",
        }
    return {
        "action": "send_nudge",
        "record_id": raw["record_id"],
        "channel": raw.get("channel", "sms"),
        "language": "hinglish",
        "message": "Aapka payment complete nahi hua — yahan click karke complete karein.",
        "customer_phone": raw.get("customer_phone"),
    }


def _build_action_params(action: str, raw: dict[str, Any], llm_result: dict[str, Any]) -> dict[str, Any]:
    """
    Merges the LLM's chosen action + qualitative content with FACTS pulled
    directly from raw_data — never from the LLM's output. This is what
    prevents a hallucinated amount or transaction ID from ever reaching
    execution: the model picks the strategy, our code supplies the numbers.
    """
    if action == "retry_payment":
        return {
            "action": "retry_payment",
            "transaction_id": raw.get("transaction_id"),
            "amount": raw.get("amount"),
            "decline_code": raw.get("decline_code"),
            "retry_attempt_number": raw.get("retry_count_so_far", 0) + 1,
        }
    if action == "retry_mandate":
        return {
            "action": "retry_mandate",
            "mandate_id": raw.get("transaction_id"),
            "amount": raw.get("amount"),
            "retry_attempt_number": raw.get("retry_count_so_far", 0) + 1,
            "mandate_state": raw.get("mandate_state", "active"),
        }
    if action == "send_payment_link":
        return {
            "action": "send_payment_link",
            "record_id": raw.get("record_id"),
            "transaction_id": raw.get("transaction_id", raw.get("record_id")),
            "amount": raw.get("amount"),
            "channel": llm_result.get("channel", "email"),
        }
    if action == "send_nudge":
        return {
            "action": "send_nudge",
            "record_id": raw.get("record_id"),
            "channel": llm_result.get("channel", raw.get("channel", "sms")),
            "language": llm_result.get("language", "hinglish"),
            "message": llm_result.get("message") or "Aapka payment complete nahi hua — yahan click karke complete karein.",
            "customer_phone": raw.get("customer_phone"),
        }
    if action == "log_promise_to_pay":
        return {
            "action": "log_promise_to_pay",
            "record_id": raw.get("record_id"),
            "promised_date": raw.get("promised_date") or llm_result.get("promised_date"),
            "amount": raw.get("amount"),
        }
    if action == "escalate_to_human":
        return {
            "action": "escalate_to_human",
            "record_id": raw.get("record_id"),
            "reason": llm_result.get("reason", "Escalated by diagnosis agent."),
            "priority": llm_result.get("priority", "medium"),
        }
    if action == "close_case":
        return {
            "action": "close_case",
            "record_id": raw.get("record_id"),
            "reason": llm_result.get("reason", "Resolved."),
        }
    # Shouldn't happen given the schema enum, but never let an unrecognized
    # action reach the Governor unvalidated — force the safe path.
    return {
        "action": "escalate_to_human",
        "record_id": raw.get("record_id"),
        "reason": f"LLM proposed unrecognized action '{action}' — safety escalation.",
        "priority": "high",
    }


async def diagnose_node(state: RecoveryState) -> RecoveryState:
    raw = state["raw_data"]
    loss_type = state["loss_type"]
    diagnosis_loss_type = loss_type if loss_type is not None else "unknown"

    taxonomy_note = None
    if loss_type == "payment_degradation" and "decline_code" in raw:
        entry = CARD_DECLINE_CODES.get(raw["decline_code"]) or UPI_DECLINE_CODES.get(raw["decline_code"])
        if entry:
            taxonomy_note = f"decline_code '{raw['decline_code']}': {entry['description']} (auto_retry_safe={entry['auto_retry_safe']})"

    context = {k: v for k, v in raw.items() if k != "record_id"}
    llm_result = await asyncio.to_thread(
        gemini_client.diagnose_and_propose, diagnosis_loss_type, context, taxonomy_note
    )

    if llm_result is not None:
        state["root_cause_code"] = llm_result.get("root_cause_code")
        state["root_cause_source"] = llm_result.get("root_cause_source")
        state["diagnosis_reasoning"] = llm_result.get("diagnosis_reasoning")
        proposed = _build_action_params(llm_result["action"], raw, llm_result)
        source_note = "real_gemini_diagnosis"
    else:
        proposed = _heuristic_propose_action(raw, loss_type)
        state["diagnosis_reasoning"] = "Gemini diagnosis unavailable — fell back to deterministic heuristic."
        source_note = "fallback_heuristic"

    state["proposed_action"] = proposed.get("action")
    state["proposed_action_params"] = proposed
    state["status"] = "diagnosed"
    append_audit(state, "diagnose", f"[{source_note}] proposed action={proposed.get('action')} — {state['diagnosis_reasoning']}")
    return state


def _make_governor_node(policy: MerchantPolicy):  # pyright: ignore[reportUnusedFunction]
    async def governor_node(state: RecoveryState) -> RecoveryState:
        proposed = state["proposed_action_params"]
        if proposed is None:
            raise ValueError("Cannot govern a state without proposed action parameters")
        raw = state["raw_data"]
        record_id = state["record_id"]

        context: dict[str, Any] = {
            "loss_type": state["loss_type"],
            "amount": raw.get("amount", 0),
            "days_overdue": raw.get("days_overdue", 0),
            "cart_value": raw.get("cart_value", raw.get("amount", 0)),
            "mandate_category": raw.get("mandate_category"),
        }

        if proposed and proposed.get("action") == "send_nudge":
            since_midnight_ist = datetime.now(MERCHANT_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
            since_midnight_utc_iso = since_midnight_ist.astimezone(timezone.utc).isoformat()
            async with persistence.atomic_record_lock(record_id):
                nudges_today = await persistence.count_nudges_today(record_id, since_midnight_utc_iso)
                context["nudges_sent_today"] = nudges_today
                context["nudges_sent_total"] = nudges_today
                context["current_hour"] = datetime.now(MERCHANT_TZ).hour

                decision = validate_action(proposed, context, policy)
                if decision.approved:
                    await persistence.record_nudge_sent(
                        record_id, proposed.get("channel", "sms"), datetime.now(timezone.utc).isoformat()
                    )
        else:
            decision = validate_action(proposed, context, policy)

        state["governor_approved"] = decision.approved
        state["governor_reasoning"] = decision.reasoning
        state["governor_blocked_reason"] = decision.blocked_reason

        if decision.approved:
            state["status"] = "policy_checked"
        else:
            blocked_reason = decision.blocked_reason or ""
            if blocked_reason.startswith("TIER1_BLOCK") or blocked_reason.startswith("SCHEMA_BLOCK"):
                state["status"] = "escalated"
            else:
                state["status"] = "unresolved"

        append_audit(
            state, "governor", decision.reasoning,
            action_taken=proposed.get("action") if decision.approved else None,
            escalated=decision.forced_escalation or (proposed.get("action") == "escalate_to_human"),
        )
        return state
    return governor_node


def governor_routing(state: RecoveryState) -> str:
    """Pure, read-only routing decision — LangGraph does NOT persist any
    state mutation made inside a conditional-edge function, only inside
    actual nodes. All status-setting for the blocked case happens in
    governor_node above; this function only reads governor_approved."""
    return "execute" if state["governor_approved"] else "end"


async def _dispatch_to_mcp(proposed: dict[str, Any]) -> dict[str, Any]:
    action = proposed["action"]

    if action == "retry_payment":
        return await mcp_razorpay.execute_retry_payment(
            transaction_id=proposed["transaction_id"], amount=proposed["amount"],
            decline_code=proposed["decline_code"], retry_attempt_number=proposed["retry_attempt_number"],
        )
    if action == "send_payment_link":
        return await mcp_razorpay.execute_send_payment_link(
            transaction_id=proposed["transaction_id"], amount=proposed["amount"], channel=proposed["channel"],
            record_id=proposed.get("record_id"), customer_contact=proposed.get("customer_phone"),
            customer_name=proposed.get("customer_name"),
        )
    if action == "retry_mandate":
        return await mcp_razorpay.execute_retry_mandate(
            mandate_id=proposed["mandate_id"], amount=proposed["amount"],
            retry_attempt_number=proposed["retry_attempt_number"],
        )
    if action == "send_nudge":
        return await mcp_comm.execute_send_nudge(
            record_id=proposed["record_id"], channel=proposed["channel"], language=proposed["language"],
            message=proposed["message"], customer_phone=proposed.get("customer_phone"),
        )
    if action == "log_promise_to_pay":
        return await mcp_ledger.execute_log_promise_to_pay(
            record_id=proposed["record_id"], promised_date=proposed["promised_date"], amount=proposed["amount"],
        )
    if action == "escalate_to_human":
        return await mcp_ledger.execute_escalate_to_human(
            record_id=proposed["record_id"],
            reason=proposed.get("reason") or "Governor-forced or diagnosis-driven escalation.",
            priority=proposed.get("priority", "medium"),
        )
    if action == "close_case":
        return await mcp_ledger.execute_close_case(record_id=proposed["record_id"], reason=proposed.get("reason", "Resolved."))

    raise ValueError(f"No MCP dispatch mapped for action '{action}'")


async def execute_node(state: RecoveryState) -> RecoveryState:
    proposed = state["proposed_action_params"]
    if proposed is None:
        raise ValueError("Cannot execute a state without proposed action parameters")
    result = await _dispatch_to_mcp(proposed)
    state["execution_result"] = result
    action = proposed["action"]
    tool_status = result.get("status")

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
        state["status"] = "executed"

    if action == "log_promise_to_pay" and tool_status == "proposed":
        await persistence.log_promise_to_pay(proposed["record_id"], proposed["promised_date"])

    append_audit(
        state, "execute", f"Dispatched {action} to MCP",
        action_taken=action, outcome=tool_status, escalated=(action == "escalate_to_human"),
    )
    return state