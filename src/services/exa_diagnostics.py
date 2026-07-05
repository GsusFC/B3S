"""Shared Exa diagnostics helpers."""

from __future__ import annotations

from typing import Any


EXA_EXTERNAL_PROOF_INTENTS = ("external_mentions", "news", "ai_visibility")


def exa_external_proof_empty(diagnostics: dict[str, Any]) -> bool:
    """True only when every observed external-proof Exa intent returned zero."""

    intent_results = diagnostics.get("intent_results")
    if not isinstance(intent_results, dict) or not intent_results:
        return False
    observed = [
        payload
        for intent, payload in intent_results.items()
        if intent in EXA_EXTERNAL_PROOF_INTENTS and isinstance(payload, dict)
    ]
    if not observed:
        return False
    return sum(int(payload.get("result_count") or 0) for payload in observed) == 0
