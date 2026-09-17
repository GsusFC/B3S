"""Helpers for normalizing multimodal vision model output."""

from __future__ import annotations

from typing import Any

PAGE_CONTENT_TYPES = frozenset({"brand_page", "interstitial", "error_page"})


def normalize_semantics_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw model fields into the Visual Signature contract."""

    visual_polish = payload.get("visual_polish")
    score = payload.get("visual_polish_score")
    rationale = payload.get("visual_polish_rationale")
    if isinstance(visual_polish, dict):
        score = visual_polish.get("score", score)
        rationale = visual_polish.get("reasoning") or visual_polish.get("rationale") or rationale

    return {
        "aesthetic_style": _non_empty_text(payload.get("aesthetic_style")),
        "visual_mood": _non_empty_text(payload.get("visual_mood")),
        "visual_polish_score": _score_or_none(score),
        "visual_polish_rationale": str(rationale or "").strip(),
        "visual_coherence": _non_empty_text(payload.get("visual_coherence")),
        "brand_distinctiveness": _non_empty_text(payload.get("brand_distinctiveness")),
        "category_fit": _non_empty_text(payload.get("category_fit")),
        "copy_visual_alignment": _non_empty_text(payload.get("copy_visual_alignment")),
        "logo_prominence": _non_empty_text(payload.get("logo_prominence")),
        "hierarchy_clarity": _non_empty_text(payload.get("hierarchy_clarity")),
        "cta_salience": _non_empty_text(payload.get("cta_salience")),
        "trust_signal_presence": _non_empty_text(payload.get("trust_signal_presence")),
        "page_content_type": _enum_or_not_detected(payload.get("page_content_type"), PAGE_CONTENT_TYPES),
        "first_impression_summary": _non_empty_text(payload.get("first_impression_summary")),
        "observed_strengths": _string_list(payload.get("observed_strengths")),
        "observed_risks": _string_list(payload.get("observed_risks")),
        "notable_absences": _string_list(payload.get("notable_absences")),
    }


def _enum_or_not_detected(value: Any, allowed: frozenset[str]) -> str:
    """Coerce an enumerated answer to the known set.

    Trust decisions read this field, so an unexpected or invented value must
    land on not_detected rather than being carried through as-is.
    """

    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return text if text in allowed else "not_detected"


def _non_empty_text(value: Any) -> str:
    text = str(value or "").strip()
    return text or "not_detected"


def _score_or_none(value: Any) -> int | None:
    try:
        score = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, min(10, score))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            values.append(text)
    return values[:8]
