"""
gemini_client.py

Wraps Gemini for the diagnose_node's real reasoning step. Uses a strict
response_schema so the model can only return one of a fixed set of
shapes — no free text to parse, no risk of it wandering off-format.

DESIGN RULE: the model's job is REASONING and QUALITATIVE content only —
root cause, which action fits, what message to send. It never restates
facts we already hold with certainty (amount, transaction_id, decline
code) in a way that gets trusted; those are always pulled from raw_data
by our own code in nodes.py's _build_action_params, never from the LLM's
output. This means a hallucinated number can never reach execution,
because the LLM is never the source of truth for numbers.
"""

from __future__ import annotations  # required: this project targets Python 3.9,
# and PEP 604 union syntax (X | Y) is evaluated eagerly and crashes on 3.9
# without this import. Keep this as the FIRST line of every .py file in
# this project — Copilot/type-checker "fixes" have introduced 3.10+-only
# syntax at least twice now; this one line makes that whole class of bug
# impossible regardless of what gets pasted in later.

import os
import json
from typing import Any, Optional
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

_client: Any = None
GEMINI_TIMEOUT_SECONDS = float(os.getenv("GEMINI_TIMEOUT_SECONDS", "20"))

VALID_ACTIONS = [
    "retry_payment", "send_payment_link", "retry_mandate",
    "send_nudge", "log_promise_to_pay", "escalate_to_human", "close_case",
]

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause_code": {"type": "string"},
        "root_cause_source": {
            "type": "string",
            "enum": ["customer", "bank", "gateway", "business", "internal", "issuer_bank"],
        },
        "diagnosis_reasoning": {"type": "string"},
        "action": {"type": "string", "enum": VALID_ACTIONS},
        "channel": {"type": "string", "enum": ["sms", "email", "whatsapp", "voice"]},
        "language": {"type": "string", "enum": ["english", "hinglish"]},
        "message": {"type": "string"},
        "reason": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
    },
    "required": ["root_cause_code", "root_cause_source", "diagnosis_reasoning", "action"],
}

# Free-tier daily quotas vary by account age and model generation — Google
# restricts some older Flash generations for newly-created API keys
# entirely (not just lower quota, a hard 404). Confirm what YOUR project
# actually has access to at https://ai.dev/rate-limit before assuming
# any model name here works — public pricing posts are not reliable for
# this, only your own account's live dashboard is.
GEMINI_MODEL = "gemini-3.5-flash-lite"


def _get_client() -> Any | None:
    global _client
    if _client is not None:
        return _client

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("[gemini_client DEBUG] GEMINI_API_KEY not found in environment")
        return None

    try:
        _client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(GEMINI_TIMEOUT_SECONDS * 1000)),
        )
    except Exception as error:
        print(f"[gemini_client DEBUG] SDK unavailable: {type(error).__name__}: {error}")
        return None
    return _client


def diagnose_and_propose(loss_type: str, record_context: dict[str, Any],
                          taxonomy_note: Optional[str] = None) -> Optional[dict[str, Any]]:
    """
    Returns the parsed LLM decision, or None on ANY failure (missing key,
    network error, malformed response, invalid action). Caller must fall
    back to the deterministic heuristic — this function never raises.
    """
    client = _get_client()
    if client is None:
        return None

    prompt = f"""You are a revenue-recovery diagnosis agent for an Indian payments platform.

Loss category: {loss_type}

Record details:
{json.dumps(record_context, indent=2)}

{f"Relevant domain context: {taxonomy_note}" if taxonomy_note else ""}

Decide:
1. The most likely root cause (a short code) and its source (customer / bank / gateway / business / internal / issuer_bank).
2. The single best recovery action from: {', '.join(VALID_ACTIONS)}.
3. If the action involves customer communication, write a short, respectful message (Hinglish for send_nudge, unless the record suggests otherwise).
4. A one-sentence plain-English reasoning for your choice.

Do not invent transaction amounts, IDs, or dates — only reason about the fields given. If in doubt between retrying and escalating to a human, prefer escalation for anything unusual, high-value, or ambiguous.
"""

    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )
        parsed = json.loads(response.text)
        if parsed.get("action") not in VALID_ACTIONS:
            print(f"[gemini_client DEBUG] invalid action in response: {parsed.get('action')}")
            return None
        return parsed
    except Exception as e:
        print(f"[gemini_client DEBUG] call failed: {type(e).__name__}: {e}")
        return None