"""
setup_subscription_demo.py

Run this ONCE, manually, outside the batch pipeline:

    python -m scripts.setup_subscription_demo

What it does:
  1. Creates a real Razorpay Plan (test mode).
  2. Creates a real Subscription against that plan — starts in 'created'
     state, meaning no payment method is attached yet.
  3. Prints a short_url. Open that URL in your browser and complete
     checkout using a Razorpay test card (e.g. 4111 1111 1111 1111,
     any future expiry, any CVV) to authenticate it.
  4. After authenticating, the subscription becomes real and 'active' —
     copy the printed subscription_id into a batch record's
     "razorpay_subscription_id" field to test retry_mandate /
     check_mandate_status against a genuinely real mandate.

Why this is a separate script and not wired into batch_runner: it needs
a human in a browser for one step. Everything else in this system is
designed to run unattended on a batch — this is the one deliberate
exception, and it's demo/setup tooling, not part of the pipeline itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.integrations import razorpay_client


def main():
    print("Creating a test Plan...")
    plan = razorpay_client.create_subscription_plan(
        period="monthly",
        interval=1,
        item_name="Revenue Recovery Demo Subscription",
        amount_inr=999.0,
    )
    print(f"  Plan created: {plan['id']}")

    print("\nCreating a Subscription against that plan...")
    subscription = razorpay_client.create_subscription(plan_id=plan["id"], total_count=12)
    print(f"  Subscription created: {subscription['id']}")
    print(f"  Status: {subscription['status']}")

    short_url = subscription.get("short_url")
    print(f"\n>>> Open this URL in your browser to authenticate it: {short_url}")
    print(">>> Use Razorpay's test card: 4111 1111 1111 1111, any future expiry, any CVV.")
    print(f"\nOnce authenticated, use this subscription_id in a batch record:")
    print(f'  "razorpay_subscription_id": "{subscription["id"]}"')


if __name__ == "__main__":
    main()