"""
twilio_client.py

Real SMS/WhatsApp sending via Twilio's trial account. Honest limitation,
worth keeping in the README: trial accounts can only send SMS to a
verified number, and WhatsApp only works through the sandbox after the
recipient has joined it. That's why this is wired for ONE demo-verified
number, not the whole synthetic batch — we don't have 50 real verified
phone numbers, and shouldn't pretend otherwise.
"""

import os
from typing import Any
from dotenv import load_dotenv

load_dotenv()

_client = None


def get_client():
    global _client
    if _client is not None:
        return _client

    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        return None

    from twilio.rest import Client  # type: ignore
    _client = Client(sid, token)
    return _client


def send_sms(to_number: str, body: str) -> dict[str, Any]:
    client = get_client()
    from_number = os.getenv("TWILIO_SMS_FROM")
    if client is None or not from_number:
        raise RuntimeError("Twilio SMS not configured — missing credentials or TWILIO_SMS_FROM")

    message = client.messages.create(body=body, from_=from_number, to=to_number)
    return {"sid": message.sid, "status": message.status, "to": to_number}


def send_whatsapp(to_number: str, body: str) -> dict[str, Any]:
    client = get_client()
    from_number = os.getenv("TWILIO_WHATSAPP_FROM")
    if client is None or not from_number:
        raise RuntimeError("Twilio WhatsApp not configured — missing credentials or TWILIO_WHATSAPP_FROM")

    message = client.messages.create(
        body=body,
        from_=f"whatsapp:{from_number}" if not from_number.startswith("whatsapp:") else from_number,
        to=f"whatsapp:{to_number}" if not to_number.startswith("whatsapp:") else to_number,
    )
    return {"sid": message.sid, "status": message.status, "to": to_number}