"""Conservative shadow producer for explicit semantic claim slots.

The producer extracts only verbatim, explicitly labelled mission and vision
statements from owned-copy evidence. Its output is persisted beside, never
inside, the evidence pack consumed by interpretation and scoring.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Any
from urllib.parse import urlparse

from src.evidence_identity import (
    normalize_evidence_text,
    normalize_evidence_url,
)
from src.sv9_flow.contracts import (
    SV9_FLOW_CANDIDATE_VERSION,
    BrandEvidencePack,
    EvidenceRecord,
)


CLAIM_SLOT_PRODUCER_VERSION = "evidence-claim-slot-producer-v2"
HISTORICAL_CLAIM_BACKFILL_VERSION = "evidence-claim-historical-backfill-v1"
_LEGACY_CANDIDATE_VERSION = "sv9-flow-candidate-v1"
_PERSISTED_DERIVATION_MODE = "persisted_shadow_lane"
_HISTORICAL_DERIVATION_MODE = "historical_backfill"
_MAX_CLAIM_CHARS = 700
_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_LOCALE_PATH_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
_DECLARATION_MARKERS: dict[str, re.Pattern[str]] = {
    "mission": re.compile(
        r"\b(?:"
        r"our mission\s+(?:is|of|to)|"
        r"nuestra misi[oó]n\s+(?:es|de|para)|"
        r"nossa miss[aã]o\s+(?:[eé]|de|para)|"
        r"mission\s*[:—-]|misi[oó]n\s*[:—-]|miss[aã]o\s*[:—-]"
        r")\s+",
        flags=re.IGNORECASE,
    ),
    "vision": re.compile(
        r"\b(?:"
        r"our vision\s+(?:is|of|to)|"
        r"nuestra visi[oó]n\s+(?:es|de|para)|"
        r"nossa vis[aã]o\s+(?:[eé]|de|para)|"
        r"vision\s*[:—-]|visi[oó]n\s*[:—-]|vis[aã]o\s*[:—-]"
        r")\s+",
        flags=re.IGNORECASE,
    ),
}
_HEADING_ROLES = {
    "mission": {
        "mission",
        "our mission",
        "mision",
        "nuestra mision",
        "missao",
        "nossa missao",
    },
    "vision": {
        "vision",
        "our vision",
        "nuestra vision",
        "visao",
        "nossa visao",
    },
}
_UNSCOPED_STRATEGIC_PATHS = {
    "about",
    "about-us",
    "company",
    "manifesto",
    "mission",
    "nossa-missao",
    "nuestra-mision",
    "our-mission",
    "our-vision",
    "quienes-somos",
    "sobre",
    "sobre-nosotros",
    "vision",
}


def build_claim_memory_evidence(
    evidence_pack: BrandEvidencePack,
    *,
    derivation_mode: str = _PERSISTED_DERIVATION_MODE,
    source_candidate_schema_version: str = "",
) -> list[EvidenceRecord]:
    """Return shadow-only semantic records from explicit owned declarations."""

    records: list[EvidenceRecord] = []
    seen: set[tuple[str, str, str]] = set()
    ordinals: Counter[tuple[str, str]] = Counter()
    for source_record in evidence_pack.evidence:
        if not _eligible_owned_record(
            source_record,
            brand_url=evidence_pack.url,
        ):
            continue
        candidates = _explicit_claim_candidates(source_record.content)
        for role, content, basis in candidates:
            normalized_content = normalize_evidence_text(content)
            if not normalized_content:
                continue
            dedupe_key = (
                role,
                normalize_evidence_url(source_record.url),
                normalized_content.casefold(),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            ordinal_key = (source_record.ref, role)
            ordinals[ordinal_key] += 1
            records.append(
                _claim_record(
                    source_record,
                    role=role,
                    content=normalized_content[:_MAX_CLAIM_CHARS],
                    basis=basis,
                    ordinal=ordinals[ordinal_key],
                    derivation_mode=derivation_mode,
                    source_candidate_schema_version=(
                        source_candidate_schema_version
                    ),
                )
            )
    return records


def resolve_candidate_claim_memory_evidence(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a persisted v2 lane or derive one from an immutable v1 pack."""

    schema_version = str(candidate.get("schema_version") or "")
    if "claim_memory_evidence" in candidate:
        raw_records = candidate.get("claim_memory_evidence")
        if not isinstance(raw_records, list):
            return _resolution(
                [],
                derivation_mode="invalid_persisted_shadow_lane",
                source_candidate_schema_version=schema_version,
            )
        records = _validated_persisted_lane(
            candidate,
            raw_records,
            schema_version=schema_version,
        )
        if records is None:
            return _resolution(
                [],
                derivation_mode="invalid_persisted_shadow_lane",
                source_candidate_schema_version=schema_version,
            )
        return _resolution(
            records,
            derivation_mode=_PERSISTED_DERIVATION_MODE,
            source_candidate_schema_version=schema_version,
        )

    if schema_version != _LEGACY_CANDIDATE_VERSION:
        return _resolution(
            [],
            derivation_mode="ineligible_candidate_schema",
            source_candidate_schema_version=schema_version,
        )

    pack_payload = (
        candidate.get("evidence_pack")
        if isinstance(candidate.get("evidence_pack"), dict)
        else {}
    )
    pack = _pack_from_payload(pack_payload)
    records = build_claim_memory_evidence(
        pack,
        derivation_mode=_HISTORICAL_DERIVATION_MODE,
        source_candidate_schema_version=schema_version,
    )
    return _resolution(
        [record.to_dict() for record in records],
        derivation_mode=_HISTORICAL_DERIVATION_MODE,
        source_candidate_schema_version=schema_version,
    )


def claim_slot_producer_summary(
    records: list[EvidenceRecord],
) -> dict[str, Any]:
    roles = Counter(
        str(record.metadata.get("claim_type") or "unknown")
        for record in records
    )
    return {
        "schema_version": CLAIM_SLOT_PRODUCER_VERSION,
        "mode": "shadow",
        "runtime_effect": False,
        "authority": False,
        "record_count": len(records),
        "claim_type_counts": dict(sorted(roles.items())),
    }


def _resolution(
    records: list[dict[str, Any]],
    *,
    derivation_mode: str,
    source_candidate_schema_version: str,
) -> dict[str, Any]:
    return {
        "schema_version": HISTORICAL_CLAIM_BACKFILL_VERSION,
        "derivation_mode": derivation_mode,
        "source_candidate_schema_version": (
            source_candidate_schema_version or "unknown"
        ),
        "record_count": len(records),
        "records": records,
        "runtime_effect": False,
        "authority": False,
    }


def _validated_persisted_lane(
    candidate: dict[str, Any],
    raw_records: list[Any],
    *,
    schema_version: str,
) -> list[dict[str, Any]] | None:
    """Verify a non-empty persisted lane against its immutable source pack."""

    # An explicit empty lane is the conservative persisted decision: it can
    # suppress a projection but cannot inject a claim.
    if not raw_records:
        return []
    if schema_version != SV9_FLOW_CANDIDATE_VERSION:
        return None
    if any(not isinstance(row, dict) for row in raw_records):
        return None
    pack_payload = candidate.get("evidence_pack")
    if not isinstance(pack_payload, dict):
        return None
    expected_records = [
        record.to_dict()
        for record in build_claim_memory_evidence(
            _pack_from_payload(pack_payload),
            source_candidate_schema_version=schema_version,
        )
    ]
    persisted_records = [dict(row) for row in raw_records]
    if persisted_records != expected_records:
        return None
    return persisted_records


def _pack_from_payload(payload: dict[str, Any]) -> BrandEvidencePack:
    evidence = [
        record
        for item in payload.get("evidence") or []
        if isinstance(item, dict)
        and (record := _record_from_payload(item)) is not None
    ]
    return BrandEvidencePack(
        brand_name=str(payload.get("brand_name") or ""),
        url=str(payload.get("url") or ""),
        evidence=evidence,
        limitations=[
            str(value)
            for value in payload.get("limitations") or []
            if str(value)
        ],
        schema_version=str(
            payload.get("schema_version") or "brand-evidence-pack-v1"
        ),
    )


def _record_from_payload(payload: dict[str, Any]) -> EvidenceRecord | None:
    content = str(payload.get("content") or "")
    if not content.strip():
        return None
    confidence = str(payload.get("confidence") or "medium").lower()
    if confidence not in {"low", "medium", "high"}:
        confidence = "medium"
    return EvidenceRecord(
        ref=str(payload.get("ref") or ""),
        source=str(payload.get("source") or ""),
        evidence_type=str(payload.get("evidence_type") or ""),
        content=content,
        url=(
            str(payload.get("url"))
            if payload.get("url") is not None
            else None
        ),
        confidence=confidence,  # type: ignore[arg-type]
        metadata=(
            dict(payload.get("metadata"))
            if isinstance(payload.get("metadata"), dict)
            else {}
        ),
    )


def _eligible_owned_record(
    record: EvidenceRecord,
    *,
    brand_url: str,
) -> bool:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    evidence_type = str(record.evidence_type or "").strip().lower()
    raw_owned_copy = evidence_type == "raw_input"
    confirmed_owned_copy = (
        evidence_type == "external_proof.owned_confirmation"
        and str(metadata.get("intent") or "").strip().lower()
        == "owned_confirmation"
        and str(metadata.get("identity_match") or "").strip().lower()
        == "domain"
    )
    if not (
        str(metadata.get("source_class") or "").strip().lower()
        == "owned_copy"
        and (raw_owned_copy or confirmed_owned_copy)
        and bool(normalize_evidence_text(record.content))
    ):
        return False
    if not _same_brand_domain(record.url, brand_url):
        return False
    if normalize_evidence_text(metadata.get("entity_scope")):
        return True
    return _is_unscoped_strategic_surface(record.url, brand_url=brand_url)


def _same_brand_domain(record_url: str | None, brand_url: str) -> bool:
    record_host = _host(record_url)
    brand_host = _host(brand_url)
    if not record_host:
        return True
    if not brand_host:
        return False
    return (
        record_host == brand_host
        or record_host.endswith(f".{brand_host}")
        or brand_host.endswith(f".{record_host}")
    )


def _is_unscoped_strategic_surface(
    record_url: str | None,
    *,
    brand_url: str,
) -> bool:
    value = str(record_url or brand_url or "").strip()
    if not value:
        return False
    if value.startswith("/"):
        parsed = urlparse(f"https://placeholder.local{value}")
    else:
        parsed = urlparse(
            value if "://" in value else f"https://{value}"
        )
    segments = [segment.casefold() for segment in parsed.path.split("/") if segment]
    if not segments:
        return True
    first = segments[0]
    if first in _UNSCOPED_STRATEGIC_PATHS:
        return True
    return bool(
        len(segments) >= 2
        and _LOCALE_PATH_RE.fullmatch(first)
        and segments[1] in _UNSCOPED_STRATEGIC_PATHS
    )


def _host(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("/", "./", "../")):
        return ""
    parsed = urlparse(
        text if "://" in text else f"https://{text.lstrip('/')}"
    )
    return str(parsed.hostname or "").casefold().removeprefix("www.")


def _explicit_claim_candidates(
    value: str,
) -> list[tuple[str, str, str]]:
    text = str(value or "").replace("\\n", "\n")
    candidates = _heading_claims(text)
    for paragraph in _paragraphs(text):
        for role, content in _statement_claims(paragraph):
            candidates.append(
                (
                    role,
                    content,
                    "explicit_owned_statement",
                )
            )
    return candidates


def _statement_claims(value: str) -> list[tuple[str, str]]:
    claims: list[tuple[str, str]] = []
    for role, pattern in _DECLARATION_MARKERS.items():
        matches = list(pattern.finditer(value))
        for index, match in enumerate(matches):
            next_marker_start = (
                matches[index + 1].start()
                if index + 1 < len(matches)
                else None
            )
            if (
                next_marker_start is not None
                and not any(
                    char in value[match.end():next_marker_start]
                    for char in ".!?#"
                )
            ):
                continue
            content = _statement_claim(
                value,
                match=match,
                next_marker_start=next_marker_start,
            )
            if content:
                claims.append(
                    (role, content)
                )
    return claims


def _statement_claim(
    value: str,
    *,
    match: re.Match[str],
    next_marker_start: int | None,
) -> str:
    tail_start = match.end()
    tail_limit = min(len(value), tail_start + _MAX_CLAIM_CHARS)
    boundaries = [
        boundary
        for boundary in (
            _first_boundary(value, tail_start, tail_limit, ".!?"),
            _first_boundary(value, tail_start, tail_limit, "#"),
            next_marker_start,
        )
        if boundary is not None and boundary >= tail_start
    ]
    tail_end = min(boundaries) if boundaries else tail_limit
    body = value[tail_start:tail_end].strip()
    if len(normalize_evidence_text(body)) < 3:
        return ""
    punctuation = (
        value[tail_end]
        if tail_end < len(value) and value[tail_end] in ".!?"
        else ""
    )
    return normalize_evidence_text(
        f"{value[match.start():match.end()]}{body}{punctuation}"
    )


def _first_boundary(
    value: str,
    start: int,
    end: int,
    boundary_chars: str,
) -> int | None:
    positions = [
        position
        for char in boundary_chars
        if (position := value.find(char, start, end)) >= 0
    ]
    return min(positions) if positions else None


def _heading_claims(text: str) -> list[tuple[str, str, str]]:
    lines = text.splitlines()
    claims: list[tuple[str, str, str]] = []
    for index, line in enumerate(lines):
        match = _MARKDOWN_HEADING_RE.match(line)
        if match is None:
            continue
        label = str(match.group(1) or "")
        heading_statements = _statement_claims(label)
        if heading_statements:
            claims.extend(
                (
                    role,
                    content,
                    "explicit_owned_heading_statement",
                )
                for role, content in heading_statements
            )
            continue
        role = _heading_role(label)
        if not role:
            continue
        paragraph: list[str] = []
        for following in lines[index + 1 :]:
            if _MARKDOWN_HEADING_RE.match(following):
                break
            cleaned = following.strip()
            if not cleaned:
                if paragraph:
                    break
                continue
            paragraph.append(cleaned)
        content = normalize_evidence_text(" ".join(paragraph))
        if content:
            claims.append(
                (role, content[:_MAX_CLAIM_CHARS], "explicit_owned_heading")
            )
    return claims


def _heading_role(value: str) -> str:
    normalized = _fold_label(value)
    for role, labels in _HEADING_ROLES.items():
        if normalized in labels:
            return role
    return ""


def _fold_label(value: str) -> str:
    folded = str(value or "").casefold()
    folded = (
        folded.replace("á", "a")
        .replace("ã", "a")
        .replace("é", "e")
        .replace("í", "i")
        .replace("ó", "o")
        .replace("õ", "o")
        .replace("ú", "u")
    )
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", folded).split())


def _paragraphs(text: str) -> list[str]:
    paragraphs: list[str] = []
    for raw_paragraph in re.split(r"\n\s*\n+", text):
        content_lines = [
            line
            for line in raw_paragraph.splitlines()
            if _MARKDOWN_HEADING_RE.match(line) is None
        ]
        paragraph = normalize_evidence_text(" ".join(content_lines))
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def _claim_record(
    source_record: EvidenceRecord,
    *,
    role: str,
    content: str,
    basis: str,
    ordinal: int,
    derivation_mode: str,
    source_candidate_schema_version: str,
) -> EvidenceRecord:
    source_metadata = (
        source_record.metadata
        if isinstance(source_record.metadata, dict)
        else {}
    )
    metadata: dict[str, Any] = {
        "source_class": "owned_copy",
        "claim_slot_key": f"{role}.primary",
        "claim_slot_key_semantics": "stable_semantic_role",
        "claim_type": role,
        "claim_slot_basis": basis,
        "claim_slot_producer_version": CLAIM_SLOT_PRODUCER_VERSION,
        "claim_slot_derivation_mode": derivation_mode,
        "historical_backfill_version": (
            HISTORICAL_CLAIM_BACKFILL_VERSION
            if derivation_mode == _HISTORICAL_DERIVATION_MODE
            else ""
        ),
        "source_candidate_schema_version": (
            source_candidate_schema_version or "unknown"
        ),
        "source_evidence_ref": source_record.ref,
        "record_ref_semantics": "provenance_only",
        "producer_occurrence": ordinal,
        "shadow_only": True,
        "runtime_effect": False,
        "authority": False,
    }
    entity_scope = normalize_evidence_text(
        source_metadata.get("entity_scope")
    )
    if entity_scope:
        metadata["entity_scope"] = entity_scope[:200]
    return EvidenceRecord(
        ref=(
            f"{'claim_memory_backfill' if derivation_mode == _HISTORICAL_DERIVATION_MODE else 'claim_memory'}."
            f"{source_record.ref}.{role}.{ordinal}"
        ),
        source=source_record.source,
        evidence_type=f"semantic_claim.{role}",
        content=content,
        url=source_record.url,
        confidence=_claim_confidence(source_record, basis=basis),
        metadata=metadata,
    )


def _claim_confidence(
    source_record: EvidenceRecord,
    *,
    basis: str,
) -> str:
    desired = (
        "high"
        if basis
        in {
            "explicit_owned_heading",
            "explicit_owned_heading_statement",
        }
        else "medium"
    )
    rank = {"low": 0, "medium": 1, "high": 2}
    source_confidence = str(source_record.confidence or "medium")
    return min(
        (desired, source_confidence),
        key=lambda value: rank.get(value, 1),
    )
