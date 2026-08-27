"""
taxonomy.py
Loads domain_taxonomy.json once at import time — used to ground the
diagnosis prompt in real decline-code meanings instead of letting the
LLM guess what a code like 'bank_technical_error' implies.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Any

_TAXONOMY_PATH = Path(__file__).parent.parent / "data" / "domain_taxonomy.json"

with open(_TAXONOMY_PATH) as f:
    _TAXONOMY = json.load(f)

CARD_DECLINE_CODES: dict[str, dict[str, Any]] = _TAXONOMY.get("card_decline_codes", {})
UPI_DECLINE_CODES: dict[str, dict[str, Any]] = _TAXONOMY.get("upi_decline_codes", {})
MANDATE_RULES: dict[str, Any] = _TAXONOMY.get("mandate_rules", {})