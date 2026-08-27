"""
razorpay_client.py

Thin wrapper around the official Razorpay Python SDK. This file only
knows how to talk to Razorpay's real test-mode API — it has no opinion
about Governor rules, fallback behavior, or what counts as "recovered."
That logic lives in mcp_razorpay.py, which calls into this file and
decides what to do if it fails.

Auth: reads RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET from the environment.
If either is missing, get_client() returns None — callers must check
for that and fall back to simulation rather than crash.
"""
from __future__ import annotations
import os
from typing import Any, Optional
from dotenv import load_dotenv

load_dotenv()

_client: "Optional[Any]" = None


def get_client() -> "Optional[Any]":
    global _client
    if _client is not None:
        return _client

    key_id = os.getenv("RAZORPAY_KEY_ID")
    key_secret = os.getenv("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        return None

    import razorpay  # type: ignore
    _client = razorpay.Client(auth=(key_id, key_secret))
    return _client


def create_order(amount_inr: float, receipt: str, currency: str = "INR") -> dict[str, Any]:
    client = get_client()
    if client is None:
        raise RuntimeError("Razorpay client not configured — missing RAZORPAY_KEY_ID/SECRET")
    client_any: Any = client
    return client_any.order.create({
        "amount": int(round(amount_inr * 100)),  # Razorpay wants paise
        "currency": currency,
        "receipt": receipt,
        "payment_capture": 1,
    })


def create_payment_link(amount_inr: float, description: str, reference_id: str,
                         customer_contact: Optional[str] = None,
                         customer_name: Optional[str] = None) -> dict[str, Any]:
    """
    Real, backend-only, no browser step required to CREATE the link.
    Customer contact is optional metadata for Razorpay's own notification —
    we don't need real customer data for the link to exist and be payable.
    """
    client = get_client()
    if client is None:
        raise RuntimeError("Razorpay client not configured — missing RAZORPAY_KEY_ID/SECRET")

    payload: dict[str, Any] = {
        "amount": int(round(amount_inr * 100)),
        "currency": "INR",
        "accept_partial": False,
        "description": description,
        "reference_id": reference_id,
        "notify": {"sms": False, "email": False},  # we control notification via our own channels
    }
    if customer_contact or customer_name:
        payload["customer"] = {
            "name": customer_name or "Customer",
            "contact": customer_contact or "",
        }
    client_any: Any = client
    return client_any.payment_link.create(payload)


def fetch_payment_link(payment_link_id: str) -> dict[str, Any]:
    client = get_client()
    if client is None:
        raise RuntimeError("Razorpay client not configured")
    client_any: Any = client
    return client_any.payment_link.fetch(payment_link_id)


def create_subscription_plan(period: str, interval: int, item_name: str,
                              amount_inr: float) -> dict[str, Any]:
    client = get_client()
    if client is None:
        raise RuntimeError("Razorpay client not configured")
    client_any: Any = client
    return client_any.plan.create({
        "period": period,       # "daily" | "weekly" | "monthly" | "yearly"
        "interval": interval,
        "item": {
            "name": item_name,
            "amount": int(round(amount_inr * 100)),
            "currency": "INR",
        },
    })


def create_subscription(plan_id: str, total_count: int = 12) -> dict[str, Any]:
    """
    Returns a subscription in 'created' state. Authenticating it (linking
    a real payment method) requires one manual browser checkout step —
    that's a Razorpay platform constraint, not something this function
    can complete on its own. This is why Subscriptions are "real for a
    demo instance," not "real across the whole batch."
    """
    client = get_client()
    if client is None:
        raise RuntimeError("Razorpay client not configured")
    client_any: Any = client
    return client_any.subscription.create({
        "plan_id": plan_id,
        "total_count": total_count,
        "customer_notify": 1,
    })


def fetch_subscription(subscription_id: str) -> dict[str, Any]:
    client = get_client()
    if client is None:
        raise RuntimeError("Razorpay client not configured")
    client_any: Any = client
    return client_any.subscription.fetch(subscription_id)


def verify_webhook_signature(payload_body: str, signature: str) -> bool:
    client = get_client()
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET")
    if client is None or not secret:
        return False
    try:
        client_any: Any = client
        client_any.utility.verify_webhook_signature(payload_body, signature, secret)
        return True
    except Exception:
        return False