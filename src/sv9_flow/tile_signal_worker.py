"""Tile signal worker for the parallel SV9 Flow."""

from __future__ import annotations

from typing import Any

from src.sv9_flow._utils import feature_confidence, truthy_detected
from src.sv9_flow.calibration_terms import (
    core_purpose_heuristics,
    magnetism_direct_pull_gap_markers,
)
from src.sv9_flow.contracts import BrandInterpretation, TileSignal

_CORE_PURPOSE_HEURISTICS = core_purpose_heuristics()
_GENERIC_CATEGORY_MARKERS = tuple(_CORE_PURPOSE_HEURISTICS["generic_category_markers"])
_RELIES_ON_STANDARD_COMPOUND = tuple(_CORE_PURPOSE_HEURISTICS["relies_on_standard_compound"])
_PRODUCT_BOUND_TERMS = tuple(_CORE_PURPOSE_HEURISTICS["product_bound_terms"])
_BEYOND_PRODUCT_TERMS = tuple(_CORE_PURPOSE_HEURISTICS["beyond_product_terms"])
_ACTION_TERMS = tuple(_CORE_PURPOSE_HEURISTICS["action_terms"])
_PRODUCT_TERMS = tuple(_CORE_PURPOSE_HEURISTICS["product_terms"])
_MISSION_VISION_OVERLAP_TERMS = tuple(_CORE_PURPOSE_HEURISTICS["mission_vision_overlap_terms"])
_MISSION_VISION_OVERLAP_GUARD_TERMS = tuple(_CORE_PURPOSE_HEURISTICS["mission_vision_overlap_guard_terms"])
_STRATEGIC_STOP_WORDS = frozenset(_CORE_PURPOSE_HEURISTICS["strategic_stop_words"])
_TOKEN_ROOTS = dict(_CORE_PURPOSE_HEURISTICS["token_roots"])
_MAGNETISM_DIRECT_PULL_GAP_MARKERS = magnetism_direct_pull_gap_markers()

# Anchor tile per canonical interpretation block. v1 signals are
# block-anchored, not per-tile; legacy block-name aliases are normalized in
# scripts/sv9_flow_legacy_compat.py before reaching this worker.
_BLOCK_TO_ANCHOR_TILE: dict[str, tuple[str, str]] = {
    "mission": ("mission", "mission.M1"),
    "vision": ("vision", "vision.V1"),
    "values": ("values", "values.VA1"),
    "attributes": ("attributes", "attributes.A1"),
    "value_proposition": ("value_proposition", "value_proposition.P1"),
    "personality": ("personality", "personality.PE1"),
    "brand_idea": ("brand_idea", "brand_idea.I1"),
    "magnetism": ("magnetism", "magnetism.MG1"),
    "core_purpose": ("core_purpose", "core_purpose.PR1"),
}


def build_tile_signals_from_interpretation(
    interpretation: BrandInterpretation,
    *,
    visual_signature_evidence: dict[str, Any] | None = None,
) -> list[TileSignal]:
    signals: list[TileSignal] = []
    for block_name, block_payload in sorted(interpretation.blocks.items()):
        component, tile = _BLOCK_TO_ANCHOR_TILE.get(block_name, (block_name, f"{block_name}.unknown"))
        detected = truthy_detected(block_payload)
        refs = interpretation.evidence_refs.get(block_name, [])
        content = str(block_payload.get("content") or "").strip()
        if detected and content:
            signals.extend(
                _detected_block_tile_signals(
                    block_name=block_name,
                    component=component,
                    tile=tile,
                    block_payload=block_payload,
                    evidence_refs=refs,
                    interpretation_limitations=interpretation.limitations,
                    sibling_blocks=interpretation.blocks,
                )
            )
        else:
            signals.append(
                TileSignal(
                    component=component,
                    tile=tile,
                    effect="insufficient_evidence",
                    confidence="low",
                    source="brand_interpretation",
                    evidence_refs=refs,
                    rationale=f"{block_name} is not sufficiently detected in brand interpretation.",
                )
            )

    signals.extend(_tile_signals_from_visual_signature(visual_signature_evidence))
    return signals


def _detected_block_tile_signals(
    *,
    block_name: str,
    component: str,
    tile: str,
    block_payload: dict[str, Any],
    evidence_refs: list[str],
    interpretation_limitations: list[str],
    sibling_blocks: dict[str, dict[str, Any]],
) -> list[TileSignal]:
    confidence = feature_confidence(block_payload.get("confidence"))
    signals = [
        TileSignal(
            component=component,
            tile=tile,
            effect="supports",
            confidence=confidence,
            source="brand_interpretation",
            evidence_refs=evidence_refs,
            rationale=f"{block_name} is detected in brand interpretation.",
        )
    ]
    if block_name == "magnetism" and _magnetism_lacks_direct_pull_evidence(interpretation_limitations):
        for blind_tile in ("magnetism.MG9", "magnetism.MG10"):
            signals.append(
                TileSignal(
                    component="magnetism",
                    tile=blind_tile,
                    effect="insufficient_evidence",
                    confidence="high",
                    source="brand_interpretation",
                    evidence_refs=evidence_refs,
                    rationale=(
                        "Flow detected magnetism but found no direct community, "
                        "developer advocacy, organic growth, or gravity evidence."
                    ),
                )
            )
    if block_name == "magnetism" and _has_limitation(
        interpretation_limitations,
        "magnetism_no_belonging_status_evidence",
    ):
        signals.append(
            TileSignal(
                component="magnetism",
                tile="magnetism.MG9",
                effect="insufficient_evidence",
                confidence="high",
                source="brand_interpretation",
                evidence_refs=evidence_refs,
                rationale="Flow detected magnetism but found no direct belonging or status evidence.",
            )
        )
    if block_name == "magnetism" and _has_limitation(interpretation_limitations, "magnetism_no_gravity_evidence"):
        signals.append(
            TileSignal(
                component="magnetism",
                tile="magnetism.MG10",
                effect="insufficient_evidence",
                confidence="high",
                source="brand_interpretation",
                evidence_refs=evidence_refs,
                rationale="Flow detected magnetism but found no direct gravity evidence.",
            )
        )
    if block_name == "magnetism" and _magnetism_lacks_owned_hook_mechanism(interpretation_limitations):
        for blind_tile in ("magnetism.MG3", "magnetism.MG4", "magnetism.MG5", "magnetism.MG6"):
            signals.append(
                TileSignal(
                    component="magnetism",
                    tile=blind_tile,
                    effect="insufficient_evidence",
                    confidence="high",
                    source="brand_interpretation",
                    evidence_refs=evidence_refs,
                    rationale=(
                        "Flow detected magnetism but found no direct evidence for this owned hook "
                        "or mechanism tile."
                    ),
                )
            )
    if block_name == "magnetism" and _magnetism_lacks_preference_mechanism(interpretation_limitations):
        for blind_tile in ("magnetism.MG7", "magnetism.MG8"):
            signals.append(
                TileSignal(
                    component="magnetism",
                    tile=blind_tile,
                    effect="insufficient_evidence",
                    confidence="high",
                    source="brand_interpretation",
                    evidence_refs=evidence_refs,
                    rationale=(
                        "Flow detected magnetism but found no direct preference or audience pull "
                        "evidence for this mechanism tile."
                    ),
                )
            )
    if block_name == "core_purpose" and _core_purpose_reads_as_generic_mission_or_offer(
        block_payload,
        interpretation_limitations,
        sibling_blocks,
    ):
        for weak_tile in (
            "core_purpose.PR3",
            "core_purpose.PR4",
            "core_purpose.PR7",
            "core_purpose.PR8",
            "core_purpose.PR9",
        ):
            signals.append(
                TileSignal(
                    component="core_purpose",
                    tile=weak_tile,
                    effect="weakens",
                    confidence="high",
                    source="brand_interpretation",
                    evidence_refs=evidence_refs,
                    rationale=(
                        "Flow core purpose appears to rest on mission, vision, or standard category benefit "
                        "language rather than distinct evidence of why the company exists beyond the product."
                    ),
                )
            )
    return signals


def _magnetism_lacks_owned_hook_mechanism(limitations: list[str]) -> bool:
    return _has_limitation(limitations, "magnetism_no_owned_hook_evidence")


def _magnetism_lacks_preference_mechanism(limitations: list[str]) -> bool:
    return _has_limitation(limitations, "magnetism_no_preference_evidence")


def _has_limitation(limitations: list[str], code: str) -> bool:
    expected = code.lower()
    return any(str(limitation or "").lower() == expected for limitation in limitations)


def _magnetism_lacks_direct_pull_evidence(limitations: list[str]) -> bool:
    for limitation in limitations:
        text = str(limitation or "").lower()
        if any(marker in text for marker in _MAGNETISM_DIRECT_PULL_GAP_MARKERS):
            return True
    return False


def _core_purpose_reads_as_generic_mission_or_offer(
    block_payload: dict[str, Any],
    limitations: list[str],
    sibling_blocks: dict[str, dict[str, Any]] | None = None,
) -> bool:
    text = " ".join(
        (
            str(block_payload.get("content") or ""),
            str(block_payload.get("rationale") or ""),
            " ".join(str(limitation or "") for limitation in limitations),
        )
    ).lower()
    if any(marker in text for marker in _GENERIC_CATEGORY_MARKERS):
        return True
    if "core_purpose_derived_strategy_evidence" in text:
        return True
    if all(term in text for term in _RELIES_ON_STANDARD_COMPOUND):
        return True
    if any(term in text for term in _PRODUCT_BOUND_TERMS) and not any(
        term in text for term in _BEYOND_PRODUCT_TERMS
    ):
        return True
    if _core_purpose_reads_as_functional_product_purpose(text) and not any(
        term in text for term in _BEYOND_PRODUCT_TERMS
    ):
        return True
    if _core_purpose_duplicates_adjacent_block(block_payload, sibling_blocks or {}) and not any(
        term in text for term in _BEYOND_PRODUCT_TERMS
    ):
        return True
    return (
        "mission" in text
        and "vision" in text
        and any(term in text for term in _MISSION_VISION_OVERLAP_TERMS)
        and not any(term in text for term in _MISSION_VISION_OVERLAP_GUARD_TERMS)
    )


def _core_purpose_reads_as_functional_product_purpose(text: str) -> bool:
    return any(term in text for term in _ACTION_TERMS) and any(term in text for term in _PRODUCT_TERMS)


def _core_purpose_duplicates_adjacent_block(
    block_payload: dict[str, Any],
    sibling_blocks: dict[str, dict[str, Any]],
) -> bool:
    purpose_tokens = _strategic_tokens(
        " ".join(
            (
                str(block_payload.get("content") or ""),
                str(block_payload.get("rationale") or ""),
            )
        )
    )
    if len(purpose_tokens) < 5:
        return False
    for sibling_name in ("mission", "value_proposition"):
        sibling = sibling_blocks.get(sibling_name)
        if not isinstance(sibling, dict):
            continue
        sibling_tokens = _strategic_tokens(
            " ".join(
                (
                    str(sibling.get("content") or ""),
                    str(sibling.get("rationale") or ""),
                )
            )
        )
        if len(sibling_tokens) < 5:
            continue
        overlap = len(purpose_tokens & sibling_tokens) / max(1, min(len(purpose_tokens), len(sibling_tokens)))
        if overlap >= 0.55:
            return True
    return False


def _strategic_tokens(text: str) -> set[str]:
    normalized = "".join(char.lower() if char.isalnum() else " " for char in text)
    return {
        _strategic_token_root(token)
        for token in normalized.split()
        if len(token) >= 4 and token not in _STRATEGIC_STOP_WORDS
    }


def _strategic_token_root(token: str) -> str:
    for prefix, root in _TOKEN_ROOTS.items():
        if token.startswith(prefix):
            return root
    if token.endswith("ies") and len(token) > 5:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 5:
        return token[:-2]
    if token.endswith("s") and len(token) > 5:
        return token[:-1]
    return token


def _tile_signals_from_visual_signature(evidence: dict[str, Any] | None) -> list[TileSignal]:
    if not isinstance(evidence, dict) or evidence.get("schema_version") != "visual-signature-evidence-v1":
        return []
    capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
    if capture.get("status") != "usable":
        return [
            TileSignal(
                component="coherencia",
                tile="coherencia.C6",
                effect="insufficient_evidence",
                confidence="high",
                source="visual_signature",
                evidence_refs=["visual_signature.capture"],
                rationale=(
                    "copy_visual_alignment_unjudgeable:"
                    f"capture_status={capture.get('status') or 'unknown'};"
                    "first_fold_evaluable="
                    f"{capture.get('first_fold_evaluable')}"
                ),
            )
        ]
    out: list[TileSignal] = []
    for index, item in enumerate(evidence.get("tile_signals") or []):
        if not isinstance(item, dict):
            continue
        tile = str(item.get("tile") or "")
        if not tile:
            continue
        component = tile.split(".", 1)[0] if "." in tile else "visual_signature"
        effect = str(item.get("effect") or "insufficient_evidence")
        if effect not in {"supports", "weakens", "insufficient_evidence", "blocked", "capture_unreliable"}:
            effect = "insufficient_evidence"
        confidence = feature_confidence(item.get("confidence"))
        if effect in {"weakens", "insufficient_evidence", "blocked"} and confidence != "high":
            # Visual acquisition is evidence, not a scorer. Low/medium negative
            # visual readings remain in the evidence pack, but they must not
            # become SV9 tile guardrails or punitively steer the evaluator.
            continue
        out.append(
            TileSignal(
                component=component,
                tile=tile,
                effect=effect,  # type: ignore[arg-type]
                confidence=confidence,
                source="visual_signature",
                evidence_refs=[f"visual_signature.tile_signals.{index}"],
                rationale=str(item.get("rationale") or "")[:700],
            )
        )
    return out
