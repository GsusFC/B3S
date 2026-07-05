"""LLM brand interpretation worker for the evidence-first SV9 Flow."""

from __future__ import annotations

import json
from typing import Any, Literal

from src.sv9_flow._utils import truthy_detected, unique_strings
from src.sv9_flow.block_detection_worker import SENSITIVE_BLOCKS, resolve_block_detection
from src.sv9_flow.calibration_terms import magnetism_families
from src.sv9_flow.block_evidence_worker import build_block_evidence_shortlists
from src.sv9_flow.contracts import BrandEvidencePack, BrandInterpretation
from src.sv9_flow.evidence_source import source_class_for_record

FLOW_INTERPRETATION_PROMPT_VERSION = "sv9-flow-brand-interpretation-v1"
_BLOCK_MAX_TOKENS = 1800
GateAuthority = Literal["veto_only", "warn", "disabled"]

_BLOCK_KEYS = (
    "mission",
    "vision",
    "values",
    "attributes",
    "value_proposition",
    "personality",
    "brand_idea",
    "core_purpose",
    "magnetism",
)

_BLOCK_GUIDANCE: dict[str, list[str]] = {
    "mission": [
        "A mission can be explicitly stated or embodied in the product's repeatable strategic mechanism.",
        "For product-embodied mission, require literal evidence of what the product converts, enables, rewards, or changes for users; do not accept a generic product category description.",
        "Keep mission distinct from value_proposition: mission explains the strategic purpose or change; value_proposition explains the direct benefit.",
    ],
    "brand_idea": [
        "Look for ownable language, distinctive methodology, named concepts, repeated phrases, and vocabulary that could summarize the brand's core idea.",
        "Do not infer brand_idea from visual style alone when textual differentiation evidence is weak.",
    ],
    "magnetism": [
        "Magnetism is market pull. Third-party external proof is valid and often the strongest grounds: press coverage, funding announcements, partnerships, customer adoption stories, analyst or industry coverage, and developer-ecosystem traction.",
        "When external_proof evidence supports real-world pull, set detected=true and cite those refs. Owned copy is not required to confirm magnetism.",
        "A brand's own marketing claims about traction are weaker evidence than third-party proof of the same traction.",
    ],
    "value_proposition": [
        "Look for who gets what concrete value: measurable business outcomes, financial value, capital, investment, speed, or operational benefit.",
        "Prefer a concrete offer statement over a broad mission statement.",
    ],
}

_BLOCK_EXAMPLES: dict[str, list[dict[str, Any]]] = {
    "mission": [
        {
            "label": "positive",
            "reason": "The evidence describes a product-embodied strategic purpose with a literal mechanism.",
            "evidence": "Acme turns every field visit into verified maintenance credit for technicians.",
            "output": {
                "detected": True,
                "content": "Turn field work into verified credit for technicians.",
                "confidence": "high",
                "evidence_refs": ["example.ref"],
                "rationale": "The evidence states what the product converts for users.",
                "limitations": [],
            },
        },
        {
            "label": "negative",
            "reason": "A category or store description is not enough to establish mission.",
            "evidence": "Acme sells cycling shoes, jackets, and sports accessories online.",
            "output": {
                "detected": False,
                "content": "",
                "confidence": "low",
                "evidence_refs": [],
                "rationale": "The evidence describes inventory rather than strategic purpose.",
                "limitations": ["mission_requires_purpose_or_product_embodied_strategy"],
            },
        },
    ],
    "brand_idea": [
        {
            "label": "positive",
            "reason": "The evidence names a repeatable idea or vocabulary the brand could own.",
            "evidence": "Acme calls its approach 'Revenue Clarity OS' across the homepage.",
            "output": {
                "detected": True,
                "content": "Revenue Clarity OS as the organizing idea for finance teams.",
                "confidence": "high",
                "evidence_refs": ["example.ref"],
                "rationale": "The named concept appears as ownable language in the evidence.",
                "limitations": [],
            },
        },
        {
            "label": "negative",
            "reason": "Visual polish alone is not a brand idea.",
            "evidence": "The screenshot is minimal, blue, and professional.",
            "output": {
                "detected": False,
                "content": "",
                "confidence": "low",
                "evidence_refs": [],
                "rationale": "The evidence describes style but not a distinctive brand idea.",
                "limitations": ["brand_idea_requires_textual_or_conceptual_evidence"],
            },
        },
    ],
    "value_proposition": [
        {
            "label": "positive",
            "reason": "The evidence states who receives what concrete benefit.",
            "evidence": "Acme helps CFOs recover 12 hours a month and defend board forecasts.",
            "output": {
                "detected": True,
                "content": "CFOs recover time and defend forecasts with clearer financial evidence.",
                "confidence": "high",
                "evidence_refs": ["example.ref"],
                "rationale": "The evidence names the audience and a concrete business outcome.",
                "limitations": [],
            },
        },
        {
            "label": "negative",
            "reason": "A broad mission claim is not enough.",
            "evidence": "Acme exists to make business better for everyone.",
            "output": {
                "detected": False,
                "content": "",
                "confidence": "low",
                "evidence_refs": [],
                "rationale": "The statement is aspirational but lacks a concrete audience and value.",
                "limitations": ["value_proposition_requires_specific_value_evidence"],
            },
        },
    ],
}

def build_brand_interpretation_with_llm(
    evidence_pack: BrandEvidencePack,
    *,
    llm: Any,
    adjudicator_llm: Any | None = None,
    block_evidence_shortlists: dict[str, list[str]] | None = None,
    per_block: bool = True,
    gate_authority: GateAuthority | str = "veto_only",
) -> tuple[BrandInterpretation, dict[str, Any]]:
    """Build brand_interpretation_v1 directly from evidence.

    The returned debug payload is for harness comparison only; it is not a score.
    In `warn` mode, an adjudicator is required for meaningful gate-disagreement
    runs: without one, evidenced LLM detections fail open and are explicitly
    flagged as unadjudicated.
    """
    gate_authority = _gate_authority(gate_authority)

    if llm is None or not getattr(llm, "api_key", None):
        interpretation = BrandInterpretation(
            brand_name=evidence_pack.brand_name,
            url=evidence_pack.url,
            limitations=unique_strings(list(evidence_pack.limitations) + ["missing_llm_api_key"]),
        )
        return interpretation, {"status": "skipped", "reason": "missing_llm_api_key"}

    system = _system_prompt()
    shortlists = block_evidence_shortlists or build_block_evidence_shortlists(evidence_pack)
    if per_block:
        raw = _call_blocks(llm=llm, system=system, evidence_pack=evidence_pack, block_evidence_shortlists=shortlists)
    else:
        user = _user_prompt(evidence_pack, block_evidence_shortlists=shortlists)
        raw = llm._call_json(
            system,
            user,
            max_tokens=3500,
            json_schema=brand_interpretation_response_schema(),
            schema_name="sv9_flow_brand_interpretation",
            strict_schema=False,
        )
    normalized = normalize_llm_interpretation_response(
        raw,
        evidence_pack,
        adjudicator_llm=adjudicator_llm,
        block_evidence_shortlists=shortlists,
        gate_authority=gate_authority,
    )
    raw_payload = _coerce_response_object(raw)
    raw_blocks = raw_payload.get("blocks") if isinstance(raw_payload.get("blocks"), dict) else {}
    detected_count = sum(
        1
        for block in normalized.blocks.values()
        if isinstance(block, dict) and block.get("detected") is True
    )
    failed_blocks = [
        key
        for key in _BLOCK_KEYS
        if not isinstance(raw_blocks.get(key), dict) or not raw_blocks.get(key)
    ]
    block_failures = _block_failures(
        raw_blocks=raw_blocks,
        normalized_blocks=normalized.blocks,
        shortlists=shortlists,
    )
    debug = {
        "status": "ok" if raw_blocks and detected_count else "empty",
        "prompt_version": FLOW_INTERPRETATION_PROMPT_VERSION,
        "shortlist_version": "sv9-flow-block-evidence-shortlists-v1",
        "gate_authority": gate_authority,
        "shortlisted_blocks": sorted(shortlists.keys()),
        "mode": "per_block" if per_block else "all_blocks",
        "detected_count": detected_count,
        "failed_blocks": failed_blocks,
        "block_failures": block_failures,
        "block_detection_decisions": _block_detection_decisions(
            evidence_pack=evidence_pack,
            shortlists=shortlists,
        ),
        "detection_provenance": {
            key: block.get("detection_provenance")
            for key, block in sorted(normalized.blocks.items())
            if isinstance(block, dict) and isinstance(block.get("detection_provenance"), dict)
        },
        "gate_disagreements": _gate_disagreements(normalized.blocks),
        "block_call_debug": raw_payload.get("_block_call_debug", []),
        "raw": raw,
    }
    failure_reason = getattr(llm, "last_failure_reason", None)
    if failure_reason:
        debug["failure_reason"] = failure_reason
    call_failures = getattr(llm, "call_failures", None)
    if isinstance(call_failures, list) and call_failures:
        latest = call_failures[-1]
        if isinstance(latest, dict):
            debug["failure"] = {
                "reason": latest.get("reason"),
                "error_type": latest.get("error_type"),
                "provider_status": latest.get("provider_status"),
                "response_empty": latest.get("response_empty"),
            }
    return normalized, debug


def normalize_llm_interpretation_response(
    raw: Any,
    evidence_pack: BrandEvidencePack,
    *,
    adjudicator_llm: Any | None = None,
    block_evidence_shortlists: dict[str, list[str]] | None = None,
    gate_authority: GateAuthority | str = "veto_only",
) -> BrandInterpretation:
    gate_authority = _gate_authority(gate_authority)
    payload = _coerce_response_object(raw)
    blocks_payload = payload.get("blocks") if isinstance(payload.get("blocks"), dict) else {}
    blocks: dict[str, dict[str, Any]] = {}
    evidence_refs: dict[str, list[str]] = {}
    limitations = list(evidence_pack.limitations)

    for key in _BLOCK_KEYS:
        block = blocks_payload.get(key)
        if not isinstance(block, dict):
            blocks[key] = {
                "detected": False,
                "content": "",
                "confidence": "low",
                "rationale": "No interpretation returned for this block.",
                "detection_provenance": {
                    "llm_detected": False,
                    "gate_detected": None,
                    "final_detected": False,
                    "final_source": "missing_block",
                    "gate_reason": "",
                },
            }
            continue
        refs = _valid_refs(
            block.get("evidence_refs"),
            evidence_pack,
            allowed_refs=(block_evidence_shortlists or {}).get(key),
        )
        policy_refs = refs
        if key in SENSITIVE_BLOCKS and block_evidence_shortlists is not None:
            policy_refs = _valid_refs(
                block_evidence_shortlists.get(key) or [],
                evidence_pack,
                allowed_refs=block_evidence_shortlists.get(key),
            )
        decision = (
            resolve_block_detection(key, evidence_pack, evidence_refs=policy_refs)
            if gate_authority in {"veto_only", "warn"} and key in SENSITIVE_BLOCKS and policy_refs
            else None
        )
        llm_detected = truthy_detected(block)
        # Gate authority is veto-only: it can reject an LLM detection, never
        # grant one. A gate-positive/LLM-negative block stays undetected and is
        # only recorded as a review-queue candidate.
        gate_candidate_rejected = bool(decision and decision.supports_detection and not llm_detected)
        adjudication = None
        if (
            key in SENSITIVE_BLOCKS
            and policy_refs
            and refs
            and llm_detected
            and decision is not None
            and not decision.supports_detection
            and adjudicator_llm is not None
            and getattr(adjudicator_llm, "api_key", None)
        ):
            adjudication = _adjudicate_gate_rejection(
                adjudicator_llm=adjudicator_llm,
                block_name=key,
                block=block,
                evidence_pack=evidence_pack,
                evidence_refs=policy_refs,
            )
        adjudicator_supports = bool(adjudication and adjudication.get("supports_detection"))
        unadjudicated_warn_disagreement = (
            gate_authority == "warn"
            and key in SENSITIVE_BLOCKS
            and bool(policy_refs)
            and bool(refs)
            and llm_detected
            and decision is not None
            and not decision.supports_detection
            and adjudication is None
            and not _adjudicator_available(adjudicator_llm)
        )
        if gate_authority == "disabled":
            detected = llm_detected and bool(refs)
        elif key in SENSITIVE_BLOCKS and policy_refs:
            detected = llm_detected and bool(refs) and bool(
                (decision and decision.supports_detection)
                or adjudicator_supports
                or unadjudicated_warn_disagreement
            )
        else:
            detected = _detected_from_evidence_policy(key, block, policy_refs, evidence_pack)
        if truthy_detected(block) and not refs:
            limitations.append(f"{key}_dropped_missing_evidence_refs")
        elif truthy_detected(block) and block_evidence_shortlists is not None:
            raw_refs = [str(ref or "").strip() for ref in block.get("evidence_refs") or []]
            dropped = [ref for ref in raw_refs if ref and ref not in refs]
            if dropped:
                limitations.append(f"{key}_dropped_refs_outside_shortlist")
        if gate_authority in {"veto_only", "warn"} and policy_refs and key in SENSITIVE_BLOCKS and not detected:
            decision = decision or resolve_block_detection(key, evidence_pack, evidence_refs=policy_refs)
            if decision.limitation_code:
                limitations.append(decision.limitation_code)
        if unadjudicated_warn_disagreement and detected:
            limitations.append(f"{key}_gate_disagreement_unadjudicated")
        if adjudicator_supports:
            limitations.append(f"{key}_adjudicator_rescued_gate_rejection")
        elif adjudication is not None:
            limitations.append(f"{key}_adjudicator_rejected_gate_rejection")
        if gate_candidate_rejected:
            limitations.append(f"{key}_gate_positive_llm_negative")
        if detected and decision is not None and decision.supports_detection:
            if key == "magnetism":
                if _magnetism_market_momentum_only(decision):
                    limitations.append("magnetism_market_momentum_only")
                limitations.extend(_magnetism_family_limitations(decision))
        if key == "core_purpose" and detected and _core_purpose_uses_derived_strategy_evidence(
            refs,
            evidence_pack,
        ):
            limitations.append("core_purpose_derived_strategy_evidence")
        content = _block_content(block)
        final_refs = _refs_with_adjudication(refs, adjudication)
        blocks[key] = {
            "detected": detected,
            "content": content if detected else "",
            "confidence": _confidence(block.get("confidence")),
            "rationale": _block_rationale(block),
            "detection_provenance": _detection_provenance(
                key=key,
                block=block,
                decision=decision,
                detected=detected,
                policy_refs=policy_refs,
                adjudication=adjudication,
                gate_authority=gate_authority,
            ),
        }
        if not detected and content:
            # Keep the vetoed interpretation reviewable without letting it
            # read as an accepted one.
            blocks[key]["rejected_content"] = content
        if final_refs:
            evidence_refs[key] = final_refs

    response_limitations = payload.get("limitations")
    if isinstance(response_limitations, list):
        limitations.extend(str(item) for item in response_limitations if item)

    return BrandInterpretation(
        brand_name=evidence_pack.brand_name,
        url=evidence_pack.url,
        blocks=blocks,
        evidence_refs=evidence_refs,
        limitations=unique_strings(limitations),
    )


def brand_interpretation_response_schema() -> dict[str, Any]:
    block_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["detected", "content", "confidence", "evidence_refs", "rationale"],
        "properties": {
            "detected": {"type": "boolean"},
            "content": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "evidence_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "rationale": {"type": "string"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["prompt_version", "blocks", "limitations"],
        "properties": {
            "prompt_version": {"type": "string"},
            "blocks": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_BLOCK_KEYS),
                "properties": {key: block_schema for key in _BLOCK_KEYS},
            },
            "limitations": {"type": "array", "items": {"type": "string"}, "maxItems": 12},
        },
    }


def block_interpretation_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["detected", "content", "confidence", "evidence_refs", "rationale", "limitations"],
        "properties": {
            "detected": {"type": "boolean"},
            "content": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "evidence_refs": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
            "rationale": {"type": "string"},
            "limitations": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        },
    }


def _call_blocks(
    *,
    llm: Any,
    system: str,
    evidence_pack: BrandEvidencePack,
    block_evidence_shortlists: dict[str, list[str]],
) -> dict[str, Any]:
    blocks: dict[str, Any] = {}
    limitations: list[str] = []
    block_call_debug: list[dict[str, Any]] = []
    for block in _BLOCK_KEYS:
        raw_block, call_debug = _call_block_json(
            llm=llm,
            system=system,
            user=_block_user_prompt(
                evidence_pack,
                block=block,
                evidence_refs=block_evidence_shortlists.get(block, []),
            ),
        )
        call_debug["block"] = block
        block_call_debug.append(call_debug)
        block_payload = _coerce_block_object(raw_block)
        blocks[block] = block_payload
        raw_limitations = block_payload.get("limitations")
        if isinstance(raw_limitations, list):
            limitations.extend(str(item) for item in raw_limitations if item)
    return {
        "prompt_version": FLOW_INTERPRETATION_PROMPT_VERSION,
        "blocks": blocks,
        "limitations": unique_strings(limitations),
        "_block_call_debug": block_call_debug,
    }


def _call_block_json(*, llm: Any, system: str, user: str) -> tuple[Any, dict[str, Any]]:
    raw = llm._call_json(
        system,
        user,
        max_tokens=_BLOCK_MAX_TOKENS,
        json_schema=None,
        schema_name=None,
        strict_schema=False,
    )
    if _coerce_block_object(raw):
        return raw, {
            "json_mode_empty": False,
            "text_fallback_attempted": False,
            "text_fallback_empty": None,
        }
    text_call = getattr(llm, "_call", None)
    if not callable(text_call):
        return raw, {
            "json_mode_empty": True,
            "text_fallback_attempted": False,
            "text_fallback_empty": None,
            "last_failure": _latest_failure(llm),
        }
    text_raw = text_call(system, user, max_tokens=_BLOCK_MAX_TOKENS)
    text_payload = _coerce_block_object(text_raw)
    return text_raw, {
        "json_mode_empty": True,
        "text_fallback_attempted": True,
        "text_fallback_empty": not bool(text_payload),
        "last_failure": _latest_failure(llm),
        "text_excerpt": "" if text_payload else _excerpt(text_raw, max_chars=220),
    }


def _system_prompt() -> str:
    return """You are Brand3's evidence interpreter for SV9.

Return strict JSON only. Do not score the brand. Do not invent evidence.
Every detected block must cite existing evidence_refs from the provided evidence list.
If evidence is weak, set detected=false or confidence=low.
"""


def _user_prompt(
    evidence_pack: BrandEvidencePack,
    *,
    block_evidence_shortlists: dict[str, list[str]],
) -> str:
    shortlisted_refs = {
        ref
        for refs in block_evidence_shortlists.values()
        for ref in refs
    }
    evidence = [
        _classified_evidence_item(record, max_chars=500)
        for record in evidence_pack.evidence
        if record.ref in shortlisted_refs
    ]
    payload = {
        "prompt_version": FLOW_INTERPRETATION_PROMPT_VERSION,
        "task": "Build brand_interpretation_v1 from evidence only.",
        "brand": {"name": evidence_pack.brand_name, "url": evidence_pack.url},
        "required_blocks": list(_BLOCK_KEYS),
        "block_evidence_shortlists": block_evidence_shortlists,
        "evidence": evidence,
        "limitations": evidence_pack.limitations,
        "output_contract": {
            "prompt_version": FLOW_INTERPRETATION_PROMPT_VERSION,
            "blocks": {
                key: {
                    "detected": False,
                    "content": "",
                    "confidence": "low",
                    "evidence_refs": [],
                    "rationale": "",
                }
                for key in _BLOCK_KEYS
            },
            "limitations": [],
        },
        "rules": [
            "Return one JSON object with exactly these top-level keys: prompt_version, blocks, limitations.",
            "The blocks object must include every required block key, even when detected=false.",
            "Do not create a score.",
            "Do not use outside knowledge.",
            "A detected block requires at least one evidence_ref from the evidence list.",
            "For each block, only cite refs listed in block_evidence_shortlists for that block.",
            "Prefer insufficient evidence over speculation.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _block_user_prompt(
    evidence_pack: BrandEvidencePack,
    *,
    block: str,
    evidence_refs: list[str],
) -> str:
    allowed = set(evidence_refs)
    evidence = [
        _classified_evidence_item(record, max_chars=700)
        for record in evidence_pack.evidence
        if record.ref in allowed
    ]
    payload = {
        "prompt_version": FLOW_INTERPRETATION_PROMPT_VERSION,
        "task": f"Draft exactly one brand_interpretation_v1 block: {block}.",
        "brand": {"name": evidence_pack.brand_name, "url": evidence_pack.url},
        "block": block,
        "block_guidance": _BLOCK_GUIDANCE.get(block, []),
        "allowed_evidence_refs": evidence_refs,
        "evidence": evidence,
        "examples": _BLOCK_EXAMPLES.get(block, []),
        "required_json": {
            "detected": "boolean",
            "content": "string, max 280 characters, empty when detected=false",
            "confidence": "low|medium|high",
            "evidence_refs": "array of allowed_evidence_refs, empty when detected=false",
            "rationale": "string, max 220 characters",
            "limitations": "array of strings",
        },
        "rules": [
            "Return only one valid JSON object with the required_json keys.",
            "Do not create a score.",
            "Do not use outside knowledge.",
            "Only cite refs from allowed_evidence_refs.",
            "Use evidence source_class/type/intent to separate owned copy, external proof, visual signal, and acquisition metadata.",
            "If the allowed evidence is weak or missing, set detected=false.",
            "Keep content and rationale concise so the JSON completes.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _classified_evidence_item(record: Any, *, max_chars: int) -> dict[str, Any]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    return {
        "ref": record.ref,
        "source": record.source,
        "source_class": metadata.get("source_class") or source_class_for_record(record),
        "type": record.evidence_type,
        "intent": metadata.get("intent") or "",
        "result_group": metadata.get("result_group") or "",
        "content": record.content[:max_chars],
        "confidence": record.confidence,
        "url": record.url,
    }


def _block_failures(
    *,
    raw_blocks: dict[str, Any],
    normalized_blocks: dict[str, dict[str, Any]],
    shortlists: dict[str, list[str]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for block in _BLOCK_KEYS:
        raw_block = raw_blocks.get(block)
        normalized = normalized_blocks.get(block) or {}
        if not isinstance(raw_block, dict) or not raw_block:
            reason = "empty_or_parse_failed"
        elif raw_block.get("detected") is True and not normalized.get("detected"):
            reason = "dropped_by_normalization"
        elif normalized.get("detected") is True:
            continue
        else:
            reason = "insufficient_evidence"
        raw_refs = raw_block.get("evidence_refs") if isinstance(raw_block, dict) else None
        failures.append(
            {
                "block": block,
                "reason": reason,
                "refs_count": len(shortlists.get(block, [])),
                "raw_refs_count": len(raw_refs) if isinstance(raw_refs, list) else 0,
                "raw_empty": not bool(raw_block),
            }
        )
    return failures


def _block_detection_decisions(
    *,
    evidence_pack: BrandEvidencePack,
    shortlists: dict[str, list[str]],
) -> list[dict[str, object]]:
    decisions: list[dict[str, object]] = []
    for block in sorted(SENSITIVE_BLOCKS):
        refs = _valid_refs(shortlists.get(block) or [], evidence_pack, allowed_refs=shortlists.get(block))
        decisions.append(resolve_block_detection(block, evidence_pack, evidence_refs=refs).to_dict())
    return decisions


def _coerce_response_object(raw: Any) -> dict[str, Any]:
    value = raw
    for _ in range(4):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                extracted = _extract_json_object_text(value)
                if not extracted:
                    return {}
                try:
                    value = json.loads(extracted)
                except json.JSONDecodeError:
                    return {}
            continue
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
            continue
        if isinstance(value, dict):
            if isinstance(value.get("blocks"), dict):
                return value
            for key in ("payload", "content", "output", "response", "result", "data"):
                nested = value.get(key)
                if isinstance(nested, (dict, list, str)):
                    value = nested
                    break
            else:
                return value
            continue
        return {}
    return value if isinstance(value, dict) else {}


def _coerce_block_object(raw: Any) -> dict[str, Any]:
    value = raw
    for _ in range(4):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                extracted = _extract_json_object_text(value)
                if not extracted:
                    return {}
                try:
                    value = json.loads(extracted)
                except json.JSONDecodeError:
                    return {}
            continue
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
            continue
        if isinstance(value, dict):
            for key in ("payload", "content", "output", "response", "result", "data"):
                nested = value.get(key)
                if isinstance(nested, (dict, list, str)) and not {"detected", "evidence_refs"} & set(value):
                    value = nested
                    break
            else:
                return value
            continue
        return {}
    return value if isinstance(value, dict) else {}


def _extract_json_object_text(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start : end + 1]


def _latest_failure(llm: Any) -> dict[str, Any] | None:
    failures = getattr(llm, "call_failures", None)
    if not isinstance(failures, list) or not failures:
        return None
    latest = failures[-1]
    if not isinstance(latest, dict):
        return None
    return {
        "reason": latest.get("reason"),
        "error_type": latest.get("error_type"),
        "response_empty": latest.get("response_empty"),
        "json_parse_error": latest.get("json_parse_error"),
    }


def _excerpt(value: Any, *, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 1]}…"


def _valid_refs(
    raw_refs: Any,
    evidence_pack: BrandEvidencePack,
    *,
    allowed_refs: list[str] | None = None,
) -> list[str]:
    allowed = set(allowed_refs) if allowed_refs is not None else {record.ref for record in evidence_pack.evidence}
    if not isinstance(raw_refs, list):
        return []
    refs = []
    for ref in raw_refs:
        value = str(ref or "").strip()
        if value and value in allowed and value not in refs:
            refs.append(value)
    return refs[:5]


def _detected_from_evidence_policy(
    key: str,
    block: dict[str, Any],
    refs: list[str],
    evidence_pack: BrandEvidencePack,
) -> bool:
    if not truthy_detected(block) or not refs:
        return False
    return resolve_block_detection(key, evidence_pack, evidence_refs=refs).supports_detection


def _gate_authority(value: Any) -> GateAuthority:
    candidate = str(value or "").strip().lower()
    if candidate in {"veto_only", "warn", "disabled"}:
        return candidate  # type: ignore[return-value]
    return "veto_only"


def _adjudicator_available(adjudicator_llm: Any | None) -> bool:
    return bool(adjudicator_llm is not None and getattr(adjudicator_llm, "api_key", None))


def _adjudicate_gate_rejection(
    *,
    adjudicator_llm: Any,
    block_name: str,
    block: dict[str, Any],
    evidence_pack: BrandEvidencePack,
    evidence_refs: list[str],
) -> dict[str, Any]:
    raw = adjudicator_llm._call_json(
        _adjudicator_system_prompt(),
        _adjudicator_user_prompt(
            block_name=block_name,
            block=block,
            evidence_pack=evidence_pack,
            evidence_refs=evidence_refs,
        ),
        max_tokens=700,
        json_schema=None,
        schema_name=None,
        strict_schema=False,
    )
    payload = _coerce_response_object(raw)
    state = str(payload.get("state") or "").strip().lower()
    ref = str(payload.get("ref") or "").strip()
    quote = str(payload.get("quote") or "").strip()
    confidence = _confidence(payload.get("confidence"))
    inference_type = str(payload.get("inference_type") or "").strip().lower()
    reason = str(payload.get("reason") or "").strip()[:500]
    validation_error = _adjudication_validation_error(
        evidence_pack=evidence_pack,
        allowed_refs=evidence_refs,
        state=state,
        ref=ref,
        quote=quote,
        confidence=confidence,
    )
    return {
        "state": state if state in {"ok", "no", "sin_evidencia"} else "no",
        "ref": ref,
        "quote": quote,
        "confidence": confidence,
        "inference_type": inference_type if inference_type in {
            "explicit_statement",
            "product_embodied_strategy",
            "tone_inferred",
            "external_proof",
            "none",
        } else "none",
        "reason": reason,
        "validation_error": validation_error,
        "supports_detection": state == "ok" and confidence in {"medium", "high"} and not validation_error,
    }


def _adjudication_validation_error(
    *,
    evidence_pack: BrandEvidencePack,
    allowed_refs: list[str],
    state: str,
    ref: str,
    quote: str,
    confidence: str,
) -> str:
    if state != "ok":
        return ""
    if confidence == "low":
        return "low_confidence"
    if ref not in set(allowed_refs):
        return "ref_not_allowed"
    if not quote:
        return "missing_quote"
    record = next((item for item in evidence_pack.evidence if item.ref == ref), None)
    if record is None:
        return "ref_not_found"
    if quote not in record.content:
        return "quote_not_literal_substring"
    return ""


def _adjudicator_system_prompt() -> str:
    return """You are SV9's evidence adjudicator.

Return strict JSON only. Your job is to decide whether a rejected sensitive
brand block is still supported by the provided evidence. Do not score. Do not
invent. An ok decision requires a short quote copied literally from one allowed
evidence item.
"""


def _adjudicator_user_prompt(
    *,
    block_name: str,
    block: dict[str, Any],
    evidence_pack: BrandEvidencePack,
    evidence_refs: list[str],
) -> str:
    allowed = set(evidence_refs)
    evidence = [
        {
            "ref": record.ref,
            "source": record.source,
            "type": record.evidence_type,
            "url": record.url,
            "content": record.content[:1200],
        }
        for record in evidence_pack.evidence
        if record.ref in allowed
    ]
    block_rules = {
        "mission": [
            "ok may be explicit mission language or a product-embodied strategic purpose.",
            "For product-embodied strategy, require evidence of what the product repeatedly converts, enables, or exists to change for users.",
        ],
        "vision": [
            "ok requires a future direction, expansion thesis, or world-state the brand is moving toward.",
            "Do not accept a generic product description as vision.",
        ],
        "values": [
            "ok requires explicit principles, commitments, standards, or named values.",
            "Do not accept tone, adjectives, or personality as values.",
        ],
        "magnetism": [
            "ok may be external proof of traction or an owned mechanism that creates repeat use, status, reward, belonging, or preference.",
            "Product mechanics such as rewards, missions, rankings, currency, territory, or public competition can support magnetism when literal evidence shows them.",
        ],
    }.get(block_name, [])
    payload = {
        "task": "Adjudicate one SV9 sensitive block after deterministic keyword gate rejection.",
        "brand": {"name": evidence_pack.brand_name, "url": evidence_pack.url},
        "block": block_name,
        "candidate": {
            "content": _block_content(block),
            "rationale": _block_rationale(block),
            "confidence": _confidence(block.get("confidence")),
            "evidence_refs": [str(ref) for ref in block.get("evidence_refs") or []],
        },
        "allowed_evidence_refs": evidence_refs,
        "evidence": evidence,
        "block_rules": block_rules,
        "required_json": {
            "state": "ok|no|sin_evidencia",
            "quote": "literal substring copied from evidence content when state=ok; otherwise empty",
            "ref": "allowed evidence ref that contains quote when state=ok; otherwise empty",
            "reason": "short reason",
            "confidence": "low|medium|high",
            "inference_type": "explicit_statement|product_embodied_strategy|tone_inferred|external_proof|none",
        },
        "rules": [
            "Return only one JSON object with the required_json keys.",
            "Use ok only when one allowed evidence item literally supports the candidate block.",
            "The quote must be copied exactly from the evidence content.",
            "Use sin_evidencia when evidence is missing or too indirect.",
            "Use no when evidence contradicts or only supports a different block.",
            "Never use outside knowledge.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _refs_with_adjudication(refs: list[str], adjudication: dict[str, Any] | None) -> list[str]:
    final_refs = list(refs)
    if adjudication and adjudication.get("supports_detection"):
        ref = str(adjudication.get("ref") or "").strip()
        if ref and ref not in final_refs:
            final_refs.append(ref)
    return final_refs[:5]


def _detection_provenance(
    *,
    key: str,
    block: dict[str, Any],
    decision: Any,
    detected: bool,
    policy_refs: list[str],
    adjudication: dict[str, Any] | None = None,
    gate_authority: GateAuthority = "veto_only",
) -> dict[str, Any]:
    llm_detected = truthy_detected(block)
    gate_applied = gate_authority in {"veto_only", "warn"} and key in SENSITIVE_BLOCKS and bool(policy_refs)
    gate_detected = bool(decision and decision.supports_detection) if gate_applied else None
    review_queue_reason = ""
    if gate_authority == "disabled" and detected:
        final_source = "llm_classified_evidence"
    elif gate_applied and gate_detected and not llm_detected:
        final_source = "llm_rejected_gate_candidate"
        review_queue_reason = "gate_positive_llm_negative"
    elif gate_applied and llm_detected and detected and adjudication and adjudication.get("supports_detection"):
        final_source = "adjudicator_rescued_gate_rejection"
    elif gate_applied and llm_detected and detected and gate_authority == "warn" and gate_detected is False:
        final_source = "llm_unadjudicated_gate_disagreement"
    elif gate_applied and llm_detected and detected:
        final_source = "llm_confirmed_by_gate"
    elif gate_applied and llm_detected and gate_detected and not detected:
        final_source = "llm_missing_evidence_refs"
    elif gate_applied and llm_detected and not detected:
        final_source = "gate_rejected"
    elif detected:
        final_source = "llm"
    else:
        final_source = "insufficient_evidence"
    provenance: dict[str, Any] = {
        "llm_detected": llm_detected,
        "gate_detected": gate_detected,
        "final_detected": detected,
        "final_source": final_source,
        "gate_reason": _gate_reason(decision),
    }
    if review_queue_reason:
        provenance["review_queue_reason"] = review_queue_reason
    if adjudication is not None:
        provenance["adjudicator"] = {
            "state": adjudication.get("state"),
            "confidence": adjudication.get("confidence"),
            "inference_type": adjudication.get("inference_type"),
            "ref": adjudication.get("ref"),
            "quote": adjudication.get("quote"),
            "reason": adjudication.get("reason"),
            "validation_error": adjudication.get("validation_error"),
            "supports_detection": adjudication.get("supports_detection"),
        }
    return provenance


def _gate_disagreements(blocks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block_name, block in sorted(blocks.items()):
        if block_name not in SENSITIVE_BLOCKS or not isinstance(block, dict):
            continue
        provenance = block.get("detection_provenance")
        if not isinstance(provenance, dict):
            continue
        if provenance.get("llm_detected") is not True or provenance.get("gate_detected") is not False:
            continue
        adjudicator = provenance.get("adjudicator") if isinstance(provenance.get("adjudicator"), dict) else {}
        rows.append(
            {
                "block": block_name,
                "gate_reason": str(provenance.get("gate_reason") or ""),
                "adjudicator_state": adjudicator.get("state") if adjudicator else None,
                "adjudicator_validation_error": str(adjudicator.get("validation_error") or "") if adjudicator else "",
                "final_detected": provenance.get("final_detected") is True,
                "final_source": str(provenance.get("final_source") or ""),
            }
        )
    return rows


def _gate_reason(decision: Any) -> str:
    if decision is None:
        return ""
    limitation = str(getattr(decision, "limitation_code", "") or "")
    if limitation:
        return limitation
    support_terms = [str(term) for term in getattr(decision, "support_terms", []) or [] if str(term)]
    weaken_terms = [str(term) for term in getattr(decision, "weaken_terms", []) or [] if str(term)]
    if support_terms:
        return "support_terms:" + ",".join(support_terms)
    if weaken_terms:
        return "weaken_terms:" + ",".join(weaken_terms)
    return str(getattr(decision, "outcome", "") or "")


def _block_content(block: dict[str, Any]) -> str:
    return str(block.get("content") or "").strip()[:700]


def _block_rationale(block: dict[str, Any]) -> str:
    return str(block.get("rationale") or "").strip()[:700]


def _confidence(value: Any) -> str:
    candidate = str(value or "").lower()
    return candidate if candidate in {"low", "medium", "high"} else "medium"


_MAGNETISM_FAMILIES = magnetism_families()


def _magnetism_market_momentum_only(decision: Any) -> bool:
    support_terms = _decision_support_terms(decision)
    if not support_terms:
        return False
    if support_terms & _MAGNETISM_FAMILIES["direct_pull"]:
        return False
    return support_terms <= _MAGNETISM_FAMILIES["broad_market"]


def _magnetism_family_limitations(decision: Any) -> list[str]:
    support_terms = _decision_support_terms(decision)
    if not support_terms:
        return []
    limitations: list[str] = []
    if not support_terms & _MAGNETISM_FAMILIES["owned_hook"]:
        limitations.append("magnetism_no_owned_hook_evidence")
    if not support_terms & _MAGNETISM_FAMILIES["preference"]:
        limitations.append("magnetism_no_preference_evidence")
    if not support_terms & _MAGNETISM_FAMILIES["belonging_status"]:
        limitations.append("magnetism_no_belonging_status_evidence")
    if not support_terms & _MAGNETISM_FAMILIES["gravity"]:
        limitations.append("magnetism_no_gravity_evidence")
    return limitations


def _decision_support_terms(decision: Any) -> set[str]:
    return {
        str(term or "").strip().lower()
        for term in getattr(decision, "support_terms", []) or []
        if str(term or "").strip()
    }


def _core_purpose_uses_derived_strategy_evidence(
    refs: list[str],
    evidence_pack: BrandEvidencePack,
) -> bool:
    if not refs:
        return False
    by_ref = {record.ref: record for record in evidence_pack.evidence}
    records = [by_ref[ref] for ref in refs if ref in by_ref]
    if not records:
        return False
    primary_web_records = [
        record
        for record in records
        if record.source == "web" and record.evidence_type == "raw_input"
    ]
    derived_records = [
        record
        for record in records
        if record.ref.startswith("features.")
        or record.source in {"entity_research_packet", "report_narrative"}
    ]
    return bool(derived_records) and len(derived_records) >= max(2, len(records) - len(primary_web_records))
