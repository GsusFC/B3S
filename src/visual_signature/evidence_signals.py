"""Tile signals and limitations for Visual Signature evidence packets."""

from __future__ import annotations

from typing import Any

from src.visual_signature._internal.utils import dict_or_empty as _dict
from src.visual_signature._internal.utils import unique as _unique
from src.visual_signature.evidence_utils import distinctiveness_proxy, score01, string_list

VISUAL_TILE_IDS = (
    "coherencia.C6",
    "brand_idea.I1",
    "brand_idea.I2",
    "brand_idea.I3",
    "brand_idea.I6",
    "brand_idea.I7",
    "brand_idea.I8",
    "brand_idea.I9",
    "core_purpose.PR8",
    "magnetism.MG1",
    "magnetism.MG5",
    "magnetism.MG7",
)


def tile_signals(
    payload: dict[str, Any],
    *,
    capture: dict[str, Any],
    identity: dict[str, Any],
    visual_system: dict[str, Any],
    first_impression: dict[str, Any],
    copy_visual_alignment: dict[str, Any],
) -> list[dict[str, Any]]:
    if capture.get("status") != "usable":
        return [
            tile_signal(
                tile=tile,
                effect="insufficient_evidence",
                confidence="high",
                source="heuristic",
                evidence_refs=["visual_signature:capture"],
                rationale=f"capture_unreliable:{capture.get('status')}",
            )
            for tile in VISUAL_TILE_IDS
        ]

    consistency = score01(_dict(visual_system.get("consistency")).get("overall_consistency"))
    logo_confidence = score01(identity.get("confidence"))
    has_logo = bool(identity.get("logo_detected"))
    has_text_brand_mark = bool(identity.get("textual_brand_mark_detected"))
    layout = _dict(visual_system.get("layout"))
    palette = _dict(visual_system.get("palette"))
    synthesis = _dict(visual_system.get("synthesis"))
    density = str(layout.get("visual_density") or "unknown")
    layout_patterns = string_list(layout.get("layout_patterns"))
    has_navigation = bool(layout.get("has_navigation"))
    has_hero = bool(layout.get("has_hero"))
    polish_available = first_impression.get("visual_polish") is not None
    polish_score = score01(first_impression.get("visual_polish"))
    # score01 floors a missing score at 0.0, which reads as "maximally bad
    # polish" and fires a high-confidence weakens. Unknown is not zero.
    polish_reason = None if polish_available else "score_unavailable:visual_polish"
    polish_text = round(polish_score, 3) if polish_available else "unavailable"
    copy_summary = str(copy_visual_alignment.get("summary") or "")
    distinctiveness = distinctiveness_proxy(payload)
    obstruction = _dict(capture.get("obstruction"))
    obstruction_present = bool(obstruction.get("present"))
    semantics = _dict(payload.get("semantics"))
    multimodal_available = _multimodal_available(semantics)
    return [
        negative_or_threshold_signal(
            "coherencia.C6",
            score=consistency,
            source="llm_multimodal" if multimodal_available and copy_summary else "heuristic",
            evidence_refs=copy_visual_alignment.get("evidence_refs") or ["visual_signature:consistency"],
            negative_reason=None,
            unavailable_reason=(
                "first_fold_not_evaluable"
                if capture.get("first_fold_evaluable") is False
                else "multimodal_semantics_unavailable"
                if not multimodal_available
                else "copy_visual_alignment_missing"
                if not copy_summary
                else None
            ),
            rationale_detail=(
                f"copy_visual_alignment:{copy_summary or 'unknown'} "
                f"consistency:{round(consistency,3)}"
            ),
        ),
        negative_or_threshold_signal(
            "brand_idea.I1",
            score=logo_confidence if has_logo else 0.0,
            source="heuristic",
            evidence_refs=["visual_signature:identity"],
            negative_reason=None if has_logo or has_text_brand_mark else "identity_evidence_missing",
            rationale_detail=f"logo_detected:{str(has_logo).lower()} text_brand_mark:{str(has_text_brand_mark).lower()} confidence:{round(logo_confidence,3)}",
        ),
        threshold_signal("brand_idea.I2", consistency, source="heuristic", evidence_refs=["visual_signature:visual_system"], rationale_detail=f"visual_tone:{synthesis.get('visual_tone') or 'unknown'} consistency:{round(consistency,3)}"),
        negative_or_threshold_signal(
            "brand_idea.I3",
            score=polish_score,
            source="llm_multimodal" if multimodal_available else "heuristic",
            evidence_refs=["visual_signature:first_impression"],
            negative_reason=None,
            unavailable_reason="multimodal_semantics_unavailable" if not multimodal_available else polish_reason,
            rationale_detail=f"first_impression:{first_impression.get('summary') or 'unknown'} polish:{polish_text}",
        ),
        negative_or_threshold_signal(
            "brand_idea.I6",
            score=0.72 if layout_patterns else 0.35,
            source="heuristic",
            evidence_refs=["visual_signature:layout"],
            negative_reason="layout_structure_not_detected" if not layout_patterns and not has_navigation and not has_hero else None,
            rationale_detail=f"layout_patterns:{','.join(layout_patterns) if layout_patterns else 'none'}",
        ),
        negative_or_threshold_signal(
            "brand_idea.I7",
            score=0.7 if density in {"balanced", "sparse"} else 0.4,
            source="heuristic",
            evidence_refs=["visual_signature:layout"],
            negative_reason="obstructed_first_fold" if obstruction_present and capture.get("first_fold_evaluable") is False else None,
            rationale_detail=f"visual_density:{density}",
        ),
        negative_or_threshold_signal(
            "brand_idea.I8",
            score=consistency,
            source="heuristic",
            evidence_refs=["visual_signature:palette", "visual_signature:typography"],
            negative_reason="palette_typography_evidence_weak" if not string_list(palette.get("dominant_colors")) else None,
            rationale_detail=f"palette_complexity:{palette.get('palette_complexity') or 'unknown'}",
        ),
        threshold_signal("brand_idea.I9", distinctiveness, source="heuristic", evidence_refs=["visual_signature:visual_system"], rationale_detail=f"distinctiveness:{synthesis.get('distinctiveness') or 'unknown'} score:{round(distinctiveness,3)}"),
        negative_or_threshold_signal(
            "core_purpose.PR8",
            score=0.72 if copy_summary else 0.35,
            source="llm_multimodal" if copy_summary and multimodal_available else "heuristic",
            evidence_refs=copy_visual_alignment.get("evidence_refs") or ["visual_signature:layout"],
            negative_reason=None,
            unavailable_reason="multimodal_semantics_unavailable" if not multimodal_available else None,
            rationale_detail=f"copy_visual_alignment:{copy_summary or 'unknown'}",
        ),
        negative_or_threshold_signal(
            "magnetism.MG1",
            score=polish_score,
            source="llm_multimodal" if multimodal_available else "heuristic",
            evidence_refs=first_impression.get("evidence_refs") or ["visual_signature:first_impression"],
            negative_reason="first_impression_not_available" if multimodal_available and not first_impression.get("summary") and polish_score == 0.0 else None,
            unavailable_reason="multimodal_semantics_unavailable" if not multimodal_available else polish_reason,
            rationale_detail=f"visual_polish:{polish_text}",
        ),
        threshold_signal("magnetism.MG5", distinctiveness, source="heuristic", evidence_refs=["visual_signature:visual_system"], rationale_detail=f"category_fit:{synthesis.get('category_fit') or 'unknown'} distinctiveness:{round(distinctiveness,3)}"),
        negative_or_threshold_signal(
            "magnetism.MG7",
            score=polish_score if copy_summary else consistency,
            source="llm_multimodal" if copy_summary and multimodal_available else "heuristic",
            evidence_refs=["visual_signature:semantics"] if copy_summary else ["visual_signature:consistency"],
            negative_reason="copy_visual_alignment_missing" if multimodal_available and not copy_summary and consistency < 0.5 else None,
            unavailable_reason=(
                "multimodal_semantics_unavailable"
                if not multimodal_available and not copy_summary
                else polish_reason
                if copy_summary
                else None
            ),
            rationale_detail=(
                f"copy_alignment_present:{str(bool(copy_summary)).lower()}"
                + (f" polish:{polish_text}" if copy_summary else "")
            ),
        ),
    ]


def threshold_signal(tile: str, score: float, *, source: str, evidence_refs: list[str], rationale_detail: str | None = None) -> dict[str, Any]:
    bounded = max(0.0, min(1.0, float(score or 0.0)))
    if bounded >= 0.62:
        effect = "supports"
    elif bounded <= 0.42:
        effect = "weakens"
    else:
        effect = "insufficient_evidence"
    confidence = "high" if bounded >= 0.75 or bounded <= 0.25 else "medium" if bounded >= 0.55 or bounded <= 0.45 else "low"
    return tile_signal(
        tile=tile,
        effect=effect,
        confidence=confidence,
        source=source,
        evidence_refs=evidence_refs,
        rationale=f"visual_signal_score:{round(bounded, 3)}" + (f" {rationale_detail}" if rationale_detail else ""),
    )


def negative_or_threshold_signal(
    tile: str,
    *,
    score: float,
    source: str,
    evidence_refs: list[str],
    negative_reason: str | None,
    unavailable_reason: str | None = None,
    rationale_detail: str | None = None,
) -> dict[str, Any]:
    if unavailable_reason:
        return tile_signal(
            tile=tile,
            effect="insufficient_evidence",
            confidence="medium",
            source=source,
            evidence_refs=evidence_refs,
            rationale=unavailable_reason + (f" {rationale_detail}" if rationale_detail else ""),
        )
    if negative_reason:
        return tile_signal(
            tile=tile,
            effect="weakens",
            confidence="medium",
            source=source,
            evidence_refs=evidence_refs,
            rationale=negative_reason + (f" {rationale_detail}" if rationale_detail else ""),
        )
    return threshold_signal(
        tile,
        score,
        source=source,
        evidence_refs=evidence_refs,
        rationale_detail=rationale_detail,
    )


def tile_signal(
    *,
    tile: str,
    effect: str,
    confidence: str,
    source: str,
    evidence_refs: list[str],
    rationale: str,
) -> dict[str, Any]:
    return {
        "tile": tile,
        "effect": effect,
        "confidence": confidence,
        "source": source,
        "evidence_refs": _unique([str(item) for item in evidence_refs if item]),
        "rationale": rationale,
    }


def limitations(payload: dict[str, Any], capture: dict[str, Any]) -> list[str]:
    values: list[str] = []
    extraction = _dict(payload.get("extraction_confidence"))
    values.extend(str(item) for item in extraction.get("limitations") or [])
    acquisition = _dict(payload.get("acquisition"))
    values.extend(f"acquisition_error:{item}" for item in acquisition.get("errors") or [])
    values.extend(f"acquisition_warning:{item}" for item in acquisition.get("warnings") or [])
    if capture.get("status") != "usable":
        values.append(f"capture_unreliable:{capture.get('status')}")
    if capture.get("content_trust") == "untrusted":
        values.append(f"capture_content_untrusted:{capture.get('content_trust_reason') or 'unknown'}")
    if not capture.get("available"):
        values.append("screenshot_not_available")
    obstruction = _dict(capture.get("obstruction"))
    if obstruction.get("present"):
        values.append(f"visual_obstruction:{obstruction.get('type')}")
    if capture.get("first_fold_evaluable") is False:
        values.append("first_fold_not_evaluable")
    return _unique(values)


def evidence_health(
    *,
    capture: dict[str, Any],
    identity: dict[str, Any],
    visual_system: dict[str, Any],
    copy_visual_alignment: dict[str, Any],
    semantics_audit: dict[str, Any],
    limitations: list[str],
) -> dict[str, Any]:
    status = str(capture.get("status") or "unknown")
    logo_detected = bool(identity.get("logo_detected"))
    text_brand_mark_detected = bool(identity.get("textual_brand_mark_detected"))
    layout = _dict(visual_system.get("layout"))
    consistency = score01(_dict(visual_system.get("consistency")).get("overall_consistency"))
    synthesis = _dict(visual_system.get("synthesis"))
    semantic_data = _dict(semantics_audit.get("data"))
    multimodal_available = _multimodal_available(semantics_audit)
    logo_prominence = str(semantic_data.get("logo_prominence") or "not_detected")
    hierarchy_clarity = str(semantic_data.get("hierarchy_clarity") or "not_detected")
    cta_salience = str(semantic_data.get("cta_salience") or "not_detected")
    trust_signal_presence = str(semantic_data.get("trust_signal_presence") or "not_detected")

    blockers: list[str] = []
    warnings: list[str] = []
    if status in {"missing", "blocked"}:
        blockers.append(f"capture_status:{status}")
    elif status == "limited":
        warnings.append("capture_status:limited")
    if capture.get("first_fold_evaluable") is False:
        warnings.append("first_fold_not_evaluable")
    if not logo_detected and not text_brand_mark_detected:
        warnings.append("identity_evidence_weak")
    if not layout.get("has_navigation") and not string_list(layout.get("layout_patterns")):
        warnings.append("layout_structure_weak")
    if not copy_visual_alignment.get("summary"):
        warnings.append("copy_visual_alignment_missing")
    if not multimodal_available:
        warnings.append("multimodal_semantics_unavailable")
    if consistency < 0.5:
        warnings.append("visual_consistency_weak")
    if multimodal_available and logo_prominence in {"weak", "not_detected"}:
        warnings.append("logo_prominence_weak")
    if multimodal_available and hierarchy_clarity in {"weak", "mixed", "not_detected"}:
        warnings.append("hierarchy_clarity_weak")
    if multimodal_available and cta_salience in {"weak", "not_detected"}:
        warnings.append("cta_salience_weak")
    if multimodal_available and trust_signal_presence in {"weak", "not_detected"}:
        warnings.append("trust_signals_weak")
    warnings.extend(str(item) for item in limitations if item not in blockers and item not in warnings)

    if blockers:
        overall = "unreliable"
    elif warnings:
        overall = "limited"
    else:
        overall = "strong"

    return {
        "overall": overall,
        "capture_status": status,
        "identity_status": "strong" if logo_detected else "partial" if text_brand_mark_detected else "weak",
        "layout_status": "strong" if layout.get("has_navigation") or string_list(layout.get("layout_patterns")) else "weak",
        "consistency_status": "strong" if consistency >= 0.7 else "partial" if consistency >= 0.5 else "weak",
        "semantic_alignment_status": "strong" if multimodal_available and copy_visual_alignment.get("summary") else "unknown",
        "logo_prominence_status": logo_prominence,
        "hierarchy_clarity_status": hierarchy_clarity,
        "cta_salience_status": cta_salience,
        "trust_signal_status": trust_signal_presence,
        "distinctiveness_status": str(synthesis.get("distinctiveness") or "unknown"),
        "blockers": _unique(blockers),
        "warnings": _unique(warnings),
    }


def _multimodal_available(semantics: dict[str, Any]) -> bool:
    if not semantics:
        return False
    return str(semantics.get("status") or "") == "detected" and not bool(semantics.get("fallback_used"))
