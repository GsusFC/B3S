"""Narrow LLM worker for pending evidence-to-tile relation proposals.

The model may propose semantic relations only. It never emits identities,
review state, authority, tile state, score, or absence claims.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from src.sv9_flow.semantic_passages import SEMANTIC_PASSAGE_CHARS, semantic_passages


EVIDENCE_TILE_RELATION_LEGACY_PROPOSAL_VERSION = (
    "evidence-tile-relation-proposal-v2"
)
EVIDENCE_TILE_RELATION_PROPOSAL_VERSION = (
    "evidence-tile-relation-proposal-v3"
)
EVIDENCE_TILE_RELATION_POLICY_VERSION = "evidence-tile-relation-policy-v3"
_ALLOWED_POLARITIES = {"supports", "contradicts"}
_MAX_RELATIONS_PER_CALL = 120
_MAX_QUOTE_CHARS = 320


class EvidenceTileRelationProposalError(ValueError):
    """A scoped relation proposal is unavailable or violates its bounds."""


def propose_evidence_tile_relations(
    *,
    evidence_rows: list[Mapping[str, Any]],
    tile_shortlists: Mapping[str, list[Mapping[str, Any]]],
    llm: Any,
) -> dict[str, Any]:
    """Propose relations across every deterministic evidence passage."""

    if llm is None or not getattr(llm, "api_key", None):
        raise EvidenceTileRelationProposalError("relation proposer requires an LLM")
    evidence = _evidence_rows(evidence_rows)
    shortlist = _tile_shortlists(tile_shortlists, evidence_by_fingerprint=evidence)
    relations: list[dict[str, str]] = []
    discarded: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for batch_evidence, batch_shortlist in _proposal_batches(evidence, shortlist):
        raw = llm._call_json(
            _system_prompt(),
            _user_prompt(batch_evidence, batch_shortlist),
            max_tokens=6000,
            json_schema=_schema(),
            schema_name="evidence_tile_relation_proposals",
            temperature=0.0,
        )
        if (
            not isinstance(raw, Mapping)
            or not isinstance(raw.get("relations"), list)
            or len(raw["relations"]) > _MAX_RELATIONS_PER_CALL
        ):
            raise EvidenceTileRelationProposalError(
                "relation proposer returned an invalid payload"
            )
        accepted, rejected = _validate_relations(
            raw["relations"],
            evidence_by_fingerprint=batch_evidence,
            shortlist=batch_shortlist,
        )
        discarded.extend(rejected)
        for relation in accepted:
            submitted = relation.pop("_submitted_relation_fingerprint")
            key = tuple(relation[field] for field in (
                "evidence_fingerprint", "tile_id", "polarity"
            ))
            if key in seen:
                discarded.append({
                    "reason": "duplicate_relation",
                    "submitted_relation_fingerprint": submitted,
                })
            else:
                seen.add(key)
                relations.append(relation)
    return {
        "schema_version": EVIDENCE_TILE_RELATION_PROPOSAL_VERSION,
        "relations": sorted(relations, key=lambda row: (
            row["evidence_fingerprint"], row["tile_id"], row["polarity"]
        )),
        "discarded_relations": sorted(discarded, key=lambda row: (
            row["submitted_relation_fingerprint"], row["reason"]
        )),
    }


def evidence_tile_relation_call_count(
    *,
    evidence_rows: list[Mapping[str, Any]],
    tile_shortlists: Mapping[str, list[Mapping[str, Any]]],
) -> int:
    evidence = _evidence_rows(evidence_rows)
    shortlist = _tile_shortlists(tile_shortlists, evidence_by_fingerprint=evidence)
    return len(_proposal_batches(evidence, shortlist))


def _proposal_batches(evidence, shortlist):
    batches, rows, tiles, chars, pairs = [], {}, {}, 0, 0
    for fingerprint, row in sorted(evidence.items()):
        for passage in semantic_passages(str(row["content"])) if shortlist[fingerprint] else ():
            if rows and (
                fingerprint in rows
                or chars + len(passage) > SEMANTIC_PASSAGE_CHARS
                or pairs + len(shortlist[fingerprint]) > _MAX_RELATIONS_PER_CALL
            ):
                batches.append((rows, tiles))
                rows, tiles, chars, pairs = {}, {}, 0, 0
            rows[fingerprint] = {**row, "content": passage}
            tiles[fingerprint] = shortlist[fingerprint]
            chars += len(passage)
            pairs += len(shortlist[fingerprint])
    if rows:
        batches.append((rows, tiles))
    return batches


def _evidence_rows(
    rows: list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise EvidenceTileRelationProposalError("evidence row must be an object")
        fingerprint = _sha256(
            row.get("evidence_fingerprint"),
            field="evidence_fingerprint",
        )
        content = str(row.get("content") or "")
        if not content:
            raise EvidenceTileRelationProposalError(
                f"evidence {fingerprint} has no content"
            )
        if fingerprint in result:
            raise EvidenceTileRelationProposalError(
                "relation proposer received duplicate evidence"
            )
        result[fingerprint] = {
            "evidence_fingerprint": fingerprint,
            "ref": str(row.get("ref") or ""),
            "source": str(row.get("source") or ""),
            "evidence_type": str(row.get("evidence_type") or ""),
            "url": str(row.get("url") or ""),
            "content": content,
            "labels": dict(row.get("labels") or {}),
        }
    return result


def _tile_shortlists(
    rows: Mapping[str, list[Mapping[str, Any]]],
    *,
    evidence_by_fingerprint: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    if not isinstance(rows, Mapping):
        raise EvidenceTileRelationProposalError("tile shortlists must be an object")
    result: dict[str, list[dict[str, str]]] = {}
    for fingerprint, raw_tiles in rows.items():
        normalized = _sha256(fingerprint, field="shortlist evidence fingerprint")
        if normalized not in evidence_by_fingerprint:
            raise EvidenceTileRelationProposalError(
                "tile shortlist references evidence outside the plan"
            )
        if not isinstance(raw_tiles, list):
            raise EvidenceTileRelationProposalError("tile shortlist must be an array")
        seen: set[str] = set()
        tiles: list[dict[str, str]] = []
        for raw in raw_tiles:
            if not isinstance(raw, Mapping):
                raise EvidenceTileRelationProposalError("tile contract must be an object")
            tile_id = str(raw.get("tile_id") or "").strip()
            tile_key = str(raw.get("tile_key") or "").strip()
            component = str(raw.get("component_key") or "").strip()
            condition = str(raw.get("condition") or "").strip()
            if not tile_id or not tile_key or not component or tile_id in seen:
                raise EvidenceTileRelationProposalError("tile shortlist contract is invalid")
            seen.add(tile_id)
            tiles.append(
                {
                    "tile_id": tile_id,
                    "tile_key": tile_key,
                    "component_key": component,
                    "condition": condition,
                }
            )
        result[normalized] = sorted(tiles, key=lambda row: row["tile_id"])
    for fingerprint in evidence_by_fingerprint:
        result.setdefault(fingerprint, [])
    return result


def _validate_relations(
    rows: list[Any],
    *,
    evidence_by_fingerprint: Mapping[str, Mapping[str, Any]],
    shortlist: Mapping[str, list[Mapping[str, str]]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    result: list[dict[str, str]] = []
    discarded: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in rows:
        try:
            relation = _validated_relation(
                raw,
                evidence_by_fingerprint=evidence_by_fingerprint,
                shortlist=shortlist,
                seen=seen,
            )
            relation["_submitted_relation_fingerprint"] = (
                _submitted_fingerprint(raw)
            )
        except _DiscardRelation as exc:
            discarded.append(
                {
                    "reason": exc.reason,
                    "submitted_relation_fingerprint": _submitted_fingerprint(raw),
                }
            )
            continue
        result.append(relation)
    return (
        sorted(
            result,
            key=lambda row: (
                row["evidence_fingerprint"],
                row["tile_id"],
                row["polarity"],
            ),
        ),
        sorted(
            discarded,
            key=lambda row: (
                row["submitted_relation_fingerprint"],
                row["reason"],
            ),
        ),
    )


class _DiscardRelation(Exception):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _validated_relation(
    raw: Any,
    *,
    evidence_by_fingerprint: Mapping[str, Mapping[str, Any]],
    shortlist: Mapping[str, list[Mapping[str, str]]],
    seen: set[tuple[str, str, str]],
) -> dict[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "evidence_fingerprint",
        "tile_id",
        "polarity",
        "literal_quote",
        "rationale",
    }:
        raise _DiscardRelation("fields_mismatch")
    if not all(
        isinstance(raw[field], str)
        for field in (
            "evidence_fingerprint",
            "tile_id",
            "polarity",
            "literal_quote",
            "rationale",
        )
    ):
        raise _DiscardRelation("field_type_invalid")
    try:
        fingerprint = _sha256(
            raw.get("evidence_fingerprint"),
            field="relation evidence fingerprint",
        )
    except EvidenceTileRelationProposalError as exc:
        raise _DiscardRelation("evidence_outside_plan") from exc
    evidence = evidence_by_fingerprint.get(fingerprint)
    if evidence is None:
        raise _DiscardRelation("evidence_outside_plan")
    tile_id = str(raw.get("tile_id") or "").strip()
    allowed_tiles = {row["tile_id"] for row in shortlist[fingerprint]}
    if tile_id not in allowed_tiles:
        raise _DiscardRelation("tile_outside_shortlist")
    polarity = str(raw.get("polarity") or "").strip()
    if polarity not in _ALLOWED_POLARITIES:
        raise _DiscardRelation("polarity_invalid")
    quote = str(raw.get("literal_quote") or "").strip()
    if (
        len(quote) < 8
        or len(quote) > _MAX_QUOTE_CHARS
        or quote not in str(evidence["content"])
    ):
        raise _DiscardRelation("quote_not_literal")
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale or len(rationale) > 1000:
        raise _DiscardRelation("rationale_invalid")
    key = (fingerprint, tile_id, polarity)
    if key in seen:
        raise _DiscardRelation("duplicate_relation")
    seen.add(key)
    return {
        "evidence_fingerprint": fingerprint,
        "tile_id": tile_id,
        "polarity": polarity,
        "literal_quote": quote,
        "rationale": rationale,
    }


def _submitted_fingerprint(raw: Any) -> str:
    rendered = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _system_prompt() -> str:
    return (
        "You propose pending evidence-to-rubric relations for Brand3. "
        "Use only the supplied evidence and only its shortlisted tiles. "
        "Return a relation only when a literal quote materially supports or "
        "contradicts the tile condition. literal_quote must be one continuous "
        "verbatim substring copied exactly from one supplied evidence passage, "
        "including Markdown punctuation and capitalization; never join fragments, "
        "shorten with ellipses, strip formatting, or paraphrase. If no supplied "
        "continuous quote works, omit the relation. Omit "
        "uncertainty. Never infer absence, never score, and never grant authority."
    )


def _user_prompt(
    evidence: Mapping[str, Mapping[str, Any]],
    shortlist: Mapping[str, list[Mapping[str, str]]],
) -> str:
    payload = [
        {**dict(row), "allowed_tiles": shortlist[fingerprint]}
        for fingerprint, row in sorted(evidence.items())
    ]
    return (
        "Scoped evidence passages and allowed tile contracts "
        f"({EVIDENCE_TILE_RELATION_POLICY_VERSION}):\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "evidence_fingerprint": {"type": "string"},
                        "tile_id": {"type": "string"},
                        "polarity": {
                            "type": "string",
                            "enum": sorted(_ALLOWED_POLARITIES),
                        },
                        "literal_quote": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "evidence_fingerprint",
                        "tile_id",
                        "polarity",
                        "literal_quote",
                        "rationale",
                    ],
                },
            }
        },
        "required": ["relations"],
    }


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise EvidenceTileRelationProposalError(f"{field} must be a SHA-256")
    text = value
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise EvidenceTileRelationProposalError(f"{field} must be a SHA-256")
    return text


__all__ = [
    "EVIDENCE_TILE_RELATION_LEGACY_PROPOSAL_VERSION",
    "EVIDENCE_TILE_RELATION_POLICY_VERSION",
    "EVIDENCE_TILE_RELATION_PROPOSAL_VERSION",
    "EvidenceTileRelationProposalError",
    "evidence_tile_relation_call_count",
    "propose_evidence_tile_relations",
]
