"""
webhook_server.py

Standalone FastAPI app — NOT part of batch_runner.py's process. Run this
separately when you're ready to demo the real-money-recovered loop closing:

    uvicorn src.integrations.webhook_server:app --reload --port 8000

Then, only when ready to demo live:
    1. ngrok http 8000
    2. Copy the ngrok https URL into Razorpay Dashboard → Settings → Webhooks,
       pointed at /webhooks/razorpay, subscribed to "payment_link.paid".
    3. Set RAZORPAY_WEBHOOK_SECRET in .env to match what you set in the dashboard.

Until that setup is done, this server can still run locally and will just
never receive anything — it's not a dependency of the batch pipeline.
"""
from __future__ import annotations
from fastapi import FastAPI, Request, HTTPException
import json
from typing import Any

from .razorpay_client import verify_webhook_signature
from .. import persistence

app = FastAPI(title="Revenue Recovery Webhook Receiver")


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> dict[str, Any]:
    body_bytes = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not verify_webhook_signature(body_bytes.decode("utf-8"), signature):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    payload = json.loads(body_bytes)
    event = payload.get("event")

    if event == "payment_link.paid":
        entity = payload["payload"]["payment_link"]["entity"]
        amount_paid = entity["amount_paid"] / 100  # paise -> INR
        reference_id = entity.get("reference_id")  # this is our record_id, if we set it

        if reference_id:
            await persistence.mark_recovered_by_reference(
                record_id=reference_id,
                amount_recovered=amount_paid,
            )
            return {"status": "processed", "record_id": reference_id, "amount_recovered": amount_paid}

    return {"status": "ignored", "event": event}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}