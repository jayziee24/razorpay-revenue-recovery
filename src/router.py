"""
router.py

Deterministic classification — no LLM. This is the front door of the
pipeline: every record gets routed to a loss_type before any reasoning
happens downstream.

Why infer instead of just trusting raw["loss_type"]: real webhook payloads
and batch rows won't always come pre-labeled with a clean category — a
production version of this would receive a Razorpay webhook event or a
raw transaction row, not a hand-tagged synthetic record. Inferring from
the actual signals present (decline_code, mandate_state, days_overdue,
cart_value) is what a real router has to do. When an explicit loss_type
IS present (as it is in our current synthetic test records), we still
prefer it — but we check it against what the signals imply, and flag a
mismatch rather than silently trusting a possibly-wrong label.
"""
from __future__ import annotations
from typing import Any, Optional
from .core_state import LossType


def _infer_from_signals(raw: dict[str, Any]) -> Optional[LossType]:
    if "decline_code" in raw and "days_overdue" not in raw:
        return "payment_degradation"
    if "mandate_state" in raw:
        return "mandate_retry"
    if "days_overdue" in raw:
        return "receivable_overdue"
    if "promised_date" in raw:
        return "promise_to_pay"
    if "cart_value" in raw:
        return "checkout_dropoff"
    return None


def classify_loss_type(raw: dict[str, Any]) -> tuple[Optional[LossType], str]:
    """
    Returns (loss_type, reasoning). loss_type is None if the record can't
    be classified at all — that's a legitimate outcome (route to escalation
    rather than force a guess), not an error to raise.
    """
    inferred = _infer_from_signals(raw)
    explicit = raw.get("loss_type")

    if explicit and inferred and explicit != inferred:
        return explicit, (
            f"Explicit loss_type='{explicit}' used, but signals in the record "
            f"suggest '{inferred}' — data quality mismatch, worth flagging upstream."
        )
    if explicit:
        return explicit, f"Explicit loss_type='{explicit}' confirmed by record signals." if inferred == explicit \
            else f"Explicit loss_type='{explicit}' used (no contradicting signals found)."
    if inferred:
        return inferred, f"No explicit loss_type — inferred '{inferred}' from record signals."

    return None, "Could not classify from any available signal — record is unclassifiable, route to escalation."