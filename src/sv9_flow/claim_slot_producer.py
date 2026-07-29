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

from src.evidence_identity import normalize_evidence_text
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord


CLAIM_SLOT_PRODUCER_VERSION = "evidence-claim-slot-producer-v1"
_MAX_CLAIM_CHARS = 700
_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_LOCALE_PATH_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$")
_SENTENCE_DECLARATIONS: dict[str, re.Pattern[str]] = {
    "mission": re.compile(
        r"(?:^|[.!?]\s+)"
        r"(?P<claim>"
        r"(?:our mission is|nuestra misi[oó]n es|nossa miss[aã]o [eé]|"
        r"mission\s*[:—-]|misi[oó]n\s*[:—-]|miss[aã]o\s*[:—-])"
        r"\s+[^.!?\n]{3,680}(?:[.!?]|$)"
        r")",
        flags=re.IGNORECASE,
    ),
    "vision": re.compile(
        r"(?:^|[.!?]\s+)"
        r"(?P<claim>"
        r"(?:our vision is|nuestra visi[oó]n es|nossa vis[aã]o [eé]|"
        r"vision\s*[:—-]|visi[oó]n\s*[:—-]|vis[aã]o\s*[:—-])"
        r"\s+[^.!?\n]{3,680}(?:[.!?]|$)"
        r")",
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
                str(source_record.url or "").strip().casefold(),
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
                )
            )
    return records


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


def _eligible_owned_record(
    record: EvidenceRecord,
    *,
    brand_url: str,
) -> bool:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    if not (
        str(metadata.get("source_class") or "").strip().lower()
        == "owned_copy"
        and str(record.evidence_type or "").strip().lower() == "raw_input"
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
        for role, pattern in _SENTENCE_DECLARATIONS.items():
            for match in pattern.finditer(paragraph):
                candidates.append(
                    (
                        role,
                        str(match.group("claim") or ""),
                        "explicit_owned_statement",
                    )
                )
    return candidates


def _heading_claims(text: str) -> list[tuple[str, str, str]]:
    lines = text.splitlines()
    claims: list[tuple[str, str, str]] = []
    for index, line in enumerate(lines):
        match = _MARKDOWN_HEADING_RE.match(line)
        if match is None:
            continue
        role = _heading_role(str(match.group(1) or ""))
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
            f"claim_memory.{source_record.ref}.{role}.{ordinal}"
        ),
        source=source_record.source,
        evidence_type=f"semantic_claim.{role}",
        content=content,
        url=source_record.url,
        confidence=(
            "high" if basis == "explicit_owned_heading" else "medium"
        ),
        metadata=metadata,
    )
