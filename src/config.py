"""
config.py

Merchant-configurable policy (Governor Tier 2 — soft rules).
These are NOT regulatory constraints. They're business thresholds a
merchant's ops team would set. Keep this separate from governor.py's
hard rules so it's obviously the "adjustable" layer in a demo —
toggle a value here, show routing change, without touching validation logic.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MerchantPolicy:
    # Escalation thresholds
    receivable_escalation_amount_inr: float = 50_000.0   # above this, always escalate to human
    receivable_escalation_days_overdue: int = 45          # above this, always escalate

    # Communication caps (on top of any regulatory notification rules)
    max_nudges_per_day: int = 1
    max_nudges_per_case: int = 3
    quiet_hours: tuple[int, int] = (21, 8)                 # 9pm-8am, no outbound comms

    # Checkout drop-off recovery
    dropoff_min_cart_value_inr: float = 200.0              # don't bother recovering below this
    dropoff_recovery_window_hours: int = 24

    # Subscription / mandate
    mandate_business_retry_cap: int = 3                    # merchant's own cap, <= NPCI's hard cap of 3

    # Promise-to-pay
    promise_grace_period_days: int = 2                     # days after promised date before auto-escalate

    # Channel preference
    preferred_language: str = "hinglish"
    voice_enabled: bool = True


DEFAULT_POLICY = MerchantPolicy()