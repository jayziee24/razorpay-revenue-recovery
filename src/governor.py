"""
governor.py

The Governor is the single choke point between "what the LLM proposed"
and "what actually gets executed via MCP." Nothing reaches a tool call
without passing through validate_action().

Two tiers of rules, deliberately kept separate:
  TIER 1 (regulatory, hard, non-negotiable) — NPCI/RBI derived.
    These can never be overridden by merchant config.
  TIER 2 (merchant, soft, configurable) — from config.py's MerchantPolicy.
    These can be tuned live for a demo without touching Governor logic.

Design choice: Pydantic models define the SHAPE of each action (strict
typed I/O). validate_action() then checks the CONTENT against policy.
A malformed action is rejected at the schema layer before policy is
even consulted — two independent failure modes, both audited.
"""
from __future__ import annotations
from typing import Literal, Optional, Union, Any, Type, Dict, cast
from pydantic import BaseModel, Field, ValidationError

from .config import MerchantPolicy, DEFAULT_POLICY


# ---------------------------------------------------------------------------
# Action schemas (Tier 0: structural validation)
# ---------------------------------------------------------------------------

class RetryPayment(BaseModel):
    action: Literal["retry_payment"] = "retry_payment"
    transaction_id: str
    amount: float = Field(gt=0)
    decline_code: str
    retry_attempt_number: int = Field(ge=1)


class SendPaymentLink(BaseModel):
    action: Literal["send_payment_link"] = "send_payment_link"
    transaction_id: str
    amount: float = Field(gt=0)
    channel: Literal["sms", "email", "whatsapp"]


class SendNudge(BaseModel):
    action: Literal["send_nudge"] = "send_nudge"
    record_id: str
    channel: Literal["sms", "email", "whatsapp", "voice"]
    language: Literal["english", "hinglish"] = "hinglish"
    message: str = Field(min_length=1, max_length=500)


class RetryMandate(BaseModel):
    action: Literal["retry_mandate"] = "retry_mandate"
    mandate_id: str
    amount: float = Field(gt=0)
    retry_attempt_number: int = Field(ge=1)
    mandate_state: Literal["created", "authorized", "active", "paused", "revoked", "expired"]


class LogPromiseToPay(BaseModel):
    action: Literal["log_promise_to_pay"] = "log_promise_to_pay"
    record_id: str
    promised_date: str  # ISO date
    amount: float = Field(gt=0)


class EscalateToHuman(BaseModel):
    action: Literal["escalate_to_human"] = "escalate_to_human"
    record_id: str
    reason: str = Field(min_length=1)
    priority: Literal["low", "medium", "high", "urgent"] = "medium"


class CloseCase(BaseModel):
    action: Literal["close_case"] = "close_case"
    record_id: str
    reason: str


RecoveryAction = Union[
    RetryPayment, SendPaymentLink, SendNudge,
    RetryMandate, LogPromiseToPay, EscalateToHuman, CloseCase,
]

ACTION_REGISTRY: Dict[str, Type[BaseModel]] = {
    "retry_payment": RetryPayment,
    "send_payment_link": SendPaymentLink,
    "send_nudge": SendNudge,
    "retry_mandate": RetryMandate,
    "log_promise_to_pay": LogPromiseToPay,
    "escalate_to_human": EscalateToHuman,
    "close_case": CloseCase,
}


# ---------------------------------------------------------------------------
# Governor decision object
# ---------------------------------------------------------------------------

class GovernorDecision(BaseModel):
    approved: bool
    action: Optional[RecoveryAction] = None
    reasoning: str
    blocked_reason: Optional[str] = None
    forced_escalation: bool = False
    tier_triggered: Optional[Literal["schema", "tier1_regulatory", "tier2_merchant"]] = None


# ---------------------------------------------------------------------------
# Tier 1 — regulatory hard rules (never overridable)
# ---------------------------------------------------------------------------

FRAUD_DECLINE_CODES = {"payment_risk_check_failed"}
NPCI_MANDATE_RETRY_CAP = 3          # 1 original + 3 retries per NPCI (Aug 2025 rule)
TERMINAL_MANDATE_STATES = {"revoked", "expired"}
AFA_FREE_LIMIT_STANDARD = 15_000.0
AFA_FREE_LIMIT_EXTENDED = 100_000.0
AFA_EXTENDED_CATEGORIES = {"credit_card_bills", "mutual_funds", "insurance", "loan_emi"}


def _check_tier1(action: RecoveryAction, context: dict[str, Any]) -> Optional[str]:
    """Returns a blocked_reason string if Tier 1 blocks the action, else None."""

    if isinstance(action, RetryPayment):
        if action.decline_code in FRAUD_DECLINE_CODES:
            return (f"TIER1_BLOCK: decline_code='{action.decline_code}' is fraud-flagged. "
                    f"Auto-retry is never permitted on fraud-coded declines.")

    if isinstance(action, RetryMandate):
        if action.mandate_state in TERMINAL_MANDATE_STATES:
            return (f"TIER1_BLOCK: mandate_state='{action.mandate_state}' is terminal. "
                    f"No retry possible; mandate must be re-registered by customer.")
        if action.retry_attempt_number > NPCI_MANDATE_RETRY_CAP:
            return (f"TIER1_BLOCK: retry_attempt_number={action.retry_attempt_number} exceeds "
                    f"NPCI cap of {NPCI_MANDATE_RETRY_CAP} retries per cycle.")
        category = context.get("mandate_category")
        afa_limit = AFA_FREE_LIMIT_EXTENDED if category in AFA_EXTENDED_CATEGORIES else AFA_FREE_LIMIT_STANDARD
        if action.amount > afa_limit:
            return (f"TIER1_BLOCK: amount={action.amount} exceeds AFA-free limit of {afa_limit} "
                    f"for category='{category}'. Cannot silently auto-charge; requires customer AFA step.")

    return None


# ---------------------------------------------------------------------------
# Tier 2 — merchant soft rules (configurable)
# ---------------------------------------------------------------------------

def _check_tier2(action: RecoveryAction, context: dict[str, Any], policy: MerchantPolicy) -> Optional[str]:
    """Returns a blocked_reason string if Tier 2 blocks the action, else None."""

    if isinstance(action, SendNudge):
        nudges_sent_today = context.get("nudges_sent_today", 0)
        nudges_sent_total = context.get("nudges_sent_total", 0)
        if nudges_sent_today >= policy.max_nudges_per_day:
            return f"TIER2_BLOCK: daily nudge cap ({policy.max_nudges_per_day}) reached for this record."
        if nudges_sent_total >= policy.max_nudges_per_case:
            return f"TIER2_BLOCK: total nudge cap ({policy.max_nudges_per_case}) reached for this case."
        current_hour = context.get("current_hour")
        if current_hour is not None:
            start, end = policy.quiet_hours
            in_quiet = (start > end and (current_hour >= start or current_hour < end)) or \
                       (start <= end and start <= current_hour < end)
            if in_quiet:
                return f"TIER2_BLOCK: current_hour={current_hour} falls in quiet hours {policy.quiet_hours}."

    if isinstance(action, EscalateToHuman):
        return None  # escalation is always allowed

    if isinstance(action, (RetryPayment, SendPaymentLink)):
        loss_type = context.get("loss_type")
        if loss_type == "checkout_dropoff":
            cart_value = context.get("cart_value", action.amount)
            if cart_value < policy.dropoff_min_cart_value_inr:
                return (f"TIER2_BLOCK: cart_value={cart_value} below merchant's minimum recovery "
                        f"threshold of {policy.dropoff_min_cart_value_inr}.")

    if isinstance(action, EscalateToHuman) or isinstance(action, CloseCase):
        return None

    if context.get("loss_type") == "receivable_overdue":
        amount = context.get("amount", 0)
        days_overdue = context.get("days_overdue", 0)
        if amount > policy.receivable_escalation_amount_inr or days_overdue > policy.receivable_escalation_days_overdue:
            if not isinstance(action, EscalateToHuman):
                return (f"TIER2_BLOCK: amount={amount}/days_overdue={days_overdue} exceeds merchant "
                        f"escalation threshold ({policy.receivable_escalation_amount_inr} / "
                        f"{policy.receivable_escalation_days_overdue}d). Must escalate to human, not auto-chase.")

    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def validate_action(proposed_action_dict: dict[str, Any], context: dict[str, Any],
                     policy: MerchantPolicy = DEFAULT_POLICY) -> GovernorDecision:
    """
    context should include whatever fields the checks above need, e.g.:
      loss_type, decline_code, mandate_category, nudges_sent_today,
      nudges_sent_total, current_hour, cart_value, amount, days_overdue

    This is the ONLY function that should sit between an LLM's proposed
    action and an MCP tool call.
    """
    action_type = proposed_action_dict.get("action")
    if not isinstance(action_type, str):
        return GovernorDecision(
            approved=False,
            reasoning=f"Unknown action type '{action_type}'.",
            blocked_reason="SCHEMA_BLOCK: not a recognized action.",
            forced_escalation=True,
            tier_triggered="schema",
        )

    model_cls = ACTION_REGISTRY.get(action_type)

    if model_cls is None:
        return GovernorDecision(
            approved=False,
            reasoning=f"Unknown action type '{action_type}'.",
            blocked_reason="SCHEMA_BLOCK: not a recognized action.",
            forced_escalation=True,
            tier_triggered="schema",
        )

    try:
        action = cast(RecoveryAction, model_cls(**proposed_action_dict))
    except ValidationError as e:
        return GovernorDecision(
            approved=False,
            reasoning=f"Schema validation failed: {e}",
            blocked_reason=f"SCHEMA_BLOCK: {e}",
            forced_escalation=True,
            tier_triggered="schema",
        )

    tier1_block = _check_tier1(action, context)
    if tier1_block:
        return GovernorDecision(
            approved=False,
            action=action,
            reasoning=tier1_block,
            blocked_reason=tier1_block,
            forced_escalation=True,
            tier_triggered="tier1_regulatory",
        )

    tier2_block = _check_tier2(action, context, policy)
    if tier2_block:
        return GovernorDecision(
            approved=False,
            action=action,
            reasoning=tier2_block,
            blocked_reason=tier2_block,
            forced_escalation=False,
            tier_triggered="tier2_merchant",
        )

    return GovernorDecision(
        approved=True,
        action=action,
        reasoning="Passed schema validation, Tier 1 regulatory checks, and Tier 2 merchant policy checks.",
    )