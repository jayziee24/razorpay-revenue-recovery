"""
core_state.py

Defines RecoveryState — the single state object that flows through every
node in the LangGraph pipeline (Router -> Specialist Node -> Governor ->
MCP Execution -> Audit).

Kept as a TypedDict (not Pydantic) because LangGraph state objects are
plain dicts under the hood — validation happens at the Governor boundary,
not here. This node is deterministic bookkeeping only.
"""
from __future__ import annotations
from typing import TypedDict, Optional, Literal, Any
from datetime import datetime, timezone


LossType = Literal[
    "payment_degradation",
    "checkout_dropoff",
    "subscription_fail",
    "mandate_retry",
    "receivable_overdue",
    "promise_to_pay",
]

RecordStatus = Literal[
    "ingested",
    "classified",
    "diagnosed",
    "policy_checked",
    "executed",
    "escalated",
    "recovered",
    "unresolved",
    "closed",
]


class AuditEntry(TypedDict):
    timestamp: str
    node: str
    reasoning: str
    action_taken: Optional[str]
    outcome: Optional[str]
    escalated: bool


class RecoveryState(TypedDict):
    # Identity
    record_id: str
    transaction_id: Optional[str]

    # Raw input (deterministic parse, no LLM touches this)
    raw_data: dict[str, Any]

    # Routing
    loss_type: Optional[LossType]

    # Diagnosis (populated by specialist node)
    root_cause_code: Optional[str]         # e.g. "insufficient_funds"
    root_cause_source: Optional[str]       # e.g. "customer" | "bank" | "gateway"
    diagnosis_reasoning: Optional[str]

    # Proposed action (LLM output, pre-Governor)
    proposed_action: Optional[str]         # e.g. "retry_payment"
    proposed_action_params: Optional[dict[str, Any]]

    # Governor decision (post-validation)
    governor_approved: Optional[bool]
    governor_reasoning: Optional[str]
    governor_blocked_reason: Optional[str]

    # Execution result (from MCP tool call)
    execution_result: Optional[dict[str, Any]]
    amount_at_stake: float
    amount_recovered: float

    # Loop control
    iteration_count: int
    max_iterations: int

    # Bookkeeping
    status: RecordStatus
    audit_trail: list[AuditEntry]
    created_at: str
    updated_at: str


def new_recovery_state(record_id: str, raw_data: dict[str, Any], max_iterations: int = 5) -> RecoveryState:
    """Factory for a fresh RecoveryState from an ingested batch row."""
    now = datetime.now(timezone.utc).isoformat()
    return RecoveryState(
        record_id=record_id,
        transaction_id=raw_data.get("transaction_id"),
        raw_data=raw_data,
        loss_type=None,
        root_cause_code=None,
        root_cause_source=None,
        diagnosis_reasoning=None,
        proposed_action=None,
        proposed_action_params=None,
        governor_approved=None,
        governor_reasoning=None,
        governor_blocked_reason=None,
        execution_result=None,
        amount_at_stake=float(raw_data.get("amount", 0)),
        amount_recovered=0.0,
        iteration_count=0,
        max_iterations=max_iterations,
        status="ingested",
        audit_trail=[],
        created_at=now,
        updated_at=now,
    )


def append_audit(state: RecoveryState, node: str, reasoning: str,
                  action_taken: Optional[str] = None, outcome: Optional[str] = None,
                  escalated: bool = False) -> RecoveryState:
    """Append an audit entry and bump updated_at. Call this from every node."""
    entry: AuditEntry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "node": node,
        "reasoning": reasoning,
        "action_taken": action_taken,
        "outcome": outcome,
        "escalated": escalated,
    }
    state["audit_trail"].append(entry)
    state["updated_at"] = entry["timestamp"]
    return state