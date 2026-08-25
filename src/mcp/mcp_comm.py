"""
mcp_comm.py

Simulated MCP boundary for local evaluation (see mcp_razorpay.py docstring
for the full rationale — same trade-off applies here).

Design rule: no persistence writes here either. Delivery outcome is
returned; the runner decides what to do with it (e.g. calling
persistence.record_nudge_sent only after a successful dispatch).

Delivery failure is modeled too — SMS/WhatsApp/voice all have real
non-zero failure rates (bad number, opted out, carrier issue). Treating
every dispatch as guaranteed delivery would be the same "100% success"
problem as the payment retries, just in a different tool.
"""

import random
import uuid
import os
from typing import Any, Optional
import asyncio

from ..integrations import twilio_client


CHANNEL_DELIVERY_SUCCESS_RATES: dict[str, float] = {
    "sms": 0.94,
    "whatsapp": 0.90,
    "email": 0.97,
    "voice": 0.80,  # answer rate is the real bottleneck for voice
}
DEFAULT_DELIVERY_SUCCESS_RATE = 0.90


async def execute_send_nudge(record_id: str, channel: str, language: str, message: str,
                              customer_phone: Optional[str] = None) -> dict[str, Any]:
    """
    Sends for REAL only if customer_phone matches our own verified Twilio
    demo number (trial-account limitation — can't SMS/WhatsApp arbitrary
    synthetic numbers). Every other record simulates delivery using the
    weighted channel success rates, same as before.
    """
    demo_phone = os.getenv("DEMO_VERIFIED_PHONE")
    dispatch_id = f"msg_{uuid.uuid4().hex[:8]}"

    if customer_phone and demo_phone and customer_phone == demo_phone:
        try:
            if channel == "whatsapp":
                result = await asyncio.to_thread(twilio_client.send_whatsapp, customer_phone, message)
            else:
                result = await asyncio.to_thread(twilio_client.send_sms, customer_phone, message)
            return {
                "status": "dispatched",
                "action": "send_nudge",
                "dispatch_id": result["sid"],
                "record_id": record_id,
                "channel": channel,
                "language": language,
                "payload": message,
                "source": "real_twilio_api",
                "twilio_status": result["status"],
            }
        except Exception as e:
            return {
                "status": "delivery_failed",
                "action": "send_nudge",
                "dispatch_id": dispatch_id,
                "record_id": record_id,
                "channel": channel,
                "language": language,
                "payload": message,
                "source": "real_twilio_api_failed",
                "failure_reason": str(e),
            }

    await asyncio.sleep(0.05)
    delivery_rate = CHANNEL_DELIVERY_SUCCESS_RATES.get(channel, DEFAULT_DELIVERY_SUCCESS_RATE)
    delivered = random.random() < delivery_rate

    if delivered:
        return {
            "status": "dispatched",
            "action": "send_nudge",
            "dispatch_id": dispatch_id,
            "record_id": record_id,
            "channel": channel,
            "language": language,
            "payload": message,
            "source": "simulated",
            "delivery_probability_used": round(delivery_rate, 2),
        }
    return {
        "status": "delivery_failed",
        "action": "send_nudge",
        "dispatch_id": dispatch_id,
        "record_id": record_id,
        "channel": channel,
        "language": language,
        "payload": message,
        "source": "simulated",
        "failure_reason": f"channel_delivery_failure:{channel}",
        "delivery_probability_used": round(delivery_rate, 2),
    }