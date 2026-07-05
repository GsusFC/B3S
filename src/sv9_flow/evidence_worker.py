"""Evidence worker for the parallel SV9 Flow."""

from __future__ import annotations

import json
import re
from typing import Any

from src.sv9_flow.evidence_source import SOURCE_CLASS_DERIVED_STRATEGY, classify_source
from src.sv9_flow._utils import feature_confidence, first_string, unique_strings
from src.sv9_flow.contracts import BrandEvidencePack, EvidenceRecord
from src.visual_signature.acquisition_contract import is_visual_acquisition_source

_RAW_INPUT_CONTENT_CHARS = 700
_WEB_CHUNK_CHARS = 900
_WEB_CHUNK_OVERLAP = 100
_MAX_WEB_SUBPAGE_CHUNKS = 6
_BOILERPLATE_MIN_PAGES = 3
_ABSENCE_BLOCK_TERMS: dict[str, tuple[str, ...]] = {
    "values": ("values", "valores", "principles", "principios", "culture", "cultura"),
    "vision": ("vision", "visión", "future", "futuro", "ambition", "ambición", "transform"),
}
_ABSENCE_SURFACE_MARKERS = (
    "about",
    "company",
    "careers",
    "jobs",
    "culture",
    "values",
    "mission",
    "manifesto",
    "nosotros",
    "empleo",
    "cultura",
    "valores",
    "sobre-",
    "quienes",
    "quiénes",
    "filosofia",
    "filosofía",
    "manifiesto",
)
# Deliberately do not add Spanish "mision": product/gamification pages such as
# /misiones/ often use it generically and would make absence claims too strong.


def build_evidence_pack_from_snapshot(
    snapshot: dict[str, Any],
    *,
    visual_signature_evidence: dict[str, Any] | None = None,
) -> BrandEvidencePack:
    run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
    brand_name = str(run.get("brand_name") or snapshot.get("brand_name") or "")
    url = str(run.get("url") or snapshot.get("url") or "")
    limitations: list[str] = []
    records: list[EvidenceRecord] = []

    if not snapshot:
        limitations.append("missing_snapshot")
    if not brand_name:
        limitations.append("missing_brand_name")
    if not url:
        limitations.append("missing_url")

    raw_records, duplicate_count = _dedup_raw_input_records(
        _evidence_from_raw_inputs(snapshot.get("raw_inputs") or [])
    )
    records.extend(raw_records)
    records.extend(_evidence_from_acquisition_steps(snapshot.get("acquisition_steps")))
    records.extend(_evidence_from_features(snapshot.get("features") or []))
    records.extend(_evidence_from_visual_signature(visual_signature_evidence))

    if duplicate_count:
        limitations.append(f"deduplicated_raw_input_records:{duplicate_count}")
    if not records:
        limitations.append("no_evidence_records")

    return BrandEvidencePack(
        brand_name=brand_name,
        url=url,
        evidence=records,
        limitations=unique_strings(limitations),
    )


def _dedup_raw_input_records(records: list[EvidenceRecord]) -> tuple[list[EvidenceRecord], int]:
    """Drop raw-input records whose text already appeared under another ref.

    Multi-pass captures re-fetch the same pages; identical text would waste
    block shortlist slots (capped at 5 refs), so only the first occurrence
    stays citable and keeps the duplicate refs in its metadata.
    """

    kept: list[EvidenceRecord] = []
    first_by_content: dict[str, EvidenceRecord] = {}
    dropped = 0
    for record in records:
        key = " ".join(record.content.split()).lower()
        if not key:
            kept.append(record)
            continue
        existing = first_by_content.get(key)
        if existing is None:
            first_by_content[key] = record
            kept.append(record)
            continue
        dropped += 1
        duplicate_refs = existing.metadata.setdefault("duplicate_refs", [])
        if len(duplicate_refs) < 5:
            duplicate_refs.append(record.ref)
    return kept, dropped


def _evidence_from_raw_inputs(raw_inputs: list[Any]) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    for index, row in enumerate(raw_inputs):
        entry = row if isinstance(row, dict) else {}
        source = str(entry.get("source") or f"raw_input_{index}")
        if source == "screenshot_capture" or is_visual_acquisition_source(source):
            continue
        payload = _payload_dict(entry)
        if source == "web":
            records.extend(_evidence_from_web_payload(index=index, source=source, payload=payload))
            continue
        if source == "exa":
            records.extend(_evidence_from_exa_payload(index=index, source=source, payload=payload))
            continue
        if source == "searchapi":
            records.extend(_evidence_from_searchapi_payload(index=index, source=source, payload=payload))
            continue
        if source == "github":
            records.extend(_evidence_from_github_payload(index=index, source=source, payload=payload))
            continue
        url = first_string(payload.get("url"), payload.get("source_url"), payload.get("page_url"))
        text = _summarize_payload(payload)
        if not text:
            continue
        records.append(_raw_input_record(index=index, source=source, content=text, url=url))
    return records


def _evidence_from_acquisition_steps(value: Any) -> list[EvidenceRecord]:
    """Expose acquisition attempts that did not create raw input payloads."""

    if not isinstance(value, dict):
        return []
    records: list[EvidenceRecord] = []
    for source, raw_step in sorted(value.items()):
        step = raw_step if isinstance(raw_step, dict) else {}
        status = str(step.get("status") or "")
        if status not in {"skipped", "error", "failed", "no_results", "not_found"}:
            continue
        details = step.get("details") if isinstance(step.get("details"), dict) else {}
        if status == "skipped" and not details:
            continue
        intent = _intent_from_acquisition_source(str(source), details)
        records.append(
            _acquisition_attempt_record(
                ref=f"acquisition_steps.{source}",
                source=str(source),
                provider=str(source),
                intent=intent,
                status=status,
                query=str(details.get("query") or details.get("reason") or ""),
                error=str(step.get("error") or details.get("error") or ""),
            )
        )
    return records


def _intent_from_acquisition_source(source: str, details: dict[str, Any]) -> str:
    if source in {"github", "github_proof"}:
        return "repository_proof"
    if source in {"searchapi", "searchapi_fallback"}:
        intents = details.get("intents")
        if isinstance(intents, list) and intents:
            return ",".join(str(item) for item in intents if str(item).strip())
        return "external_proof"
    if source == "web":
        return "owned_surface"
    if source == "exa":
        return "external_proof"
    return source or "acquisition"


def _evidence_from_exa_payload(*, index: int, source: str, payload: dict[str, Any]) -> list[EvidenceRecord]:
    """Expose Exa results as individually citable external-proof records."""

    records: list[EvidenceRecord] = []
    groups = (
        ("mentions", "external_mentions"),
        ("profiles", "external_profiles"),
        ("news", "news"),
        ("ai_visibility_results", "ai_visibility"),
        ("competitors", "competitors"),
    )
    seen_urls: set[str] = set()
    for group, fallback_intent in groups:
        for result_index, result in enumerate(_list(payload.get(group))):
            if not isinstance(result, dict):
                continue
            content = _external_result_content(result)
            if not content:
                continue
            url = first_string(result.get("url"))
            # Exa repeats the same article across groups (e.g. mentions + news);
            # keep only the first group's record per URL.
            if url:
                url_key = url.strip().lower().rstrip("/")
                if url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
            intent = str(result.get("intent") or fallback_intent)
            source_class = "owned_copy" if intent == "owned_confirmation" else "external_proof"
            records.append(
                EvidenceRecord(
                    ref=f"raw_inputs.{index}.exa.{group}.{result_index}",
                    source=source,
                    evidence_type=f"external_proof.{intent}",
                    content=content[:_RAW_INPUT_CONTENT_CHARS],
                    url=url,
                    confidence=_external_result_confidence(result),
                    metadata={
                        "source_class": source_class,
                        "provider": "exa",
                        "intent": intent,
                        "result_group": group,
                        "score": result.get("score"),
                        "published_date": result.get("published_date") or "",
                    },
                )
            )
    records.extend(_exa_diagnostic_records(index=index, source=source, diagnostics=payload.get("diagnostics")))
    return records


def _evidence_from_searchapi_payload(*, index: int, source: str, payload: dict[str, Any]) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    intents = payload.get("intents") if isinstance(payload.get("intents"), dict) else {}
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    if not intents and str(payload.get("status") or "") in {"skipped", "error"}:
        records.append(
            _acquisition_attempt_record(
                ref=f"raw_inputs.{index}.searchapi.diagnostics.status",
                source=source,
                provider="searchapi",
                intent="external_proof",
                status=str(payload.get("status") or "unknown"),
                query="",
                error=str(diagnostics.get("reason") or ""),
            )
        )
    for intent, intent_payload in intents.items():
        if not isinstance(intent_payload, dict):
            continue
        status = str(intent_payload.get("status") or "unknown")
        if status != "ok":
            records.append(
                _acquisition_attempt_record(
                    ref=f"raw_inputs.{index}.searchapi.diagnostics.{intent}",
                    source=source,
                    provider="searchapi",
                    intent=str(intent),
                    status=status,
                    query=str(intent_payload.get("query") or ""),
                    error=str(intent_payload.get("error") or ""),
                )
            )
        for result_index, result in enumerate(_list(intent_payload.get("results"))):
            if not isinstance(result, dict):
                continue
            content = _searchapi_result_content(result)
            if not content:
                continue
            records.append(
                EvidenceRecord(
                    ref=f"raw_inputs.{index}.searchapi.{intent}.{result_index}",
                    source=source,
                    evidence_type=f"external_proof.{intent}",
                    content=content[:_RAW_INPUT_CONTENT_CHARS],
                    url=first_string(result.get("url"), result.get("link")),
                    confidence="medium",
                    metadata={
                        "source_class": "external_proof",
                        "provider": "searchapi",
                        "intent": str(intent),
                        "engine": result.get("engine") or intent_payload.get("engine") or "google_light",
                        "query": result.get("query") or intent_payload.get("query") or "",
                        "position": result.get("position") or 0,
                        "domain": result.get("domain") or "",
                    },
                )
            )
    return records


def _searchapi_result_content(result: dict[str, Any]) -> str:
    title = str(result.get("title") or "").strip()
    snippet = str(result.get("snippet") or "").strip()
    source = str(result.get("source") or result.get("domain") or "").strip()
    parts = [part for part in (title, source, snippet) if part]
    return " ".join(" ".join(part.split()) for part in parts).strip()


def _evidence_from_github_payload(*, index: int, source: str, payload: dict[str, Any]) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    for repo_index, repo in enumerate(_list(payload.get("repos"))):
        if not isinstance(repo, dict):
            continue
        full_name = str(repo.get("full_name") or "").strip()
        html_url = first_string(repo.get("html_url"))
        if not full_name and not html_url:
            continue
        content = _github_repo_content(repo)
        if not content:
            continue
        records.append(
            EvidenceRecord(
                ref=f"raw_inputs.{index}.github.repos.{repo_index}",
                source=source,
                evidence_type="external_proof.repository",
                content=content[:_RAW_INPUT_CONTENT_CHARS],
                url=html_url,
                confidence=_github_repo_confidence(repo),
                metadata={
                    "source_class": "external_proof",
                    "provider": "github",
                    "intent": "repository_proof",
                    "repo": full_name,
                    "stars": repo.get("stars") or 0,
                    "forks": repo.get("forks") or 0,
                    "topics": repo.get("topics") or [],
                    "pushed_at": repo.get("pushed_at") or "",
                },
            )
        )
    diagnostics = payload.get("diagnostics") if isinstance(payload.get("diagnostics"), dict) else {}
    status = str(payload.get("status") or "")
    if not records and status in {"skipped", "error"}:
        records.append(
            _acquisition_attempt_record(
                ref=f"raw_inputs.{index}.github.diagnostics.status",
                source=source,
                provider="github",
                intent="repository_proof",
                status=status,
                query="observed GitHub repository links on owned capture",
                error=str(diagnostics.get("reason") or ""),
            )
        )
    for error_index, error in enumerate(_list(diagnostics.get("errors"))):
        if not isinstance(error, dict):
            continue
        records.append(
            _acquisition_attempt_record(
                ref=f"raw_inputs.{index}.github.diagnostics.error.{error_index}",
                source=source,
                provider="github",
                intent="repository_proof",
                status="error",
                query=str(error.get("repo") or ""),
                error=str(error.get("error") or ""),
            )
        )
    return records


def _github_repo_content(repo: dict[str, Any]) -> str:
    full_name = str(repo.get("full_name") or "").strip()
    description = str(repo.get("description") or "").strip()
    language = str(repo.get("language") or "").strip()
    topics = ", ".join(str(item) for item in _list(repo.get("topics")) if str(item).strip())
    parts = [
        f"GitHub repository {full_name}" if full_name else "",
        description,
        f"{int(repo.get('stars') or 0)} stars",
        f"{int(repo.get('forks') or 0)} forks",
        f"{int(repo.get('open_issues') or 0)} open issues",
        f"language: {language}" if language else "",
        f"topics: {topics}" if topics else "",
        f"last pushed: {repo.get('pushed_at')}" if repo.get("pushed_at") else "",
    ]
    return ". ".join(part for part in parts if part).strip()


def _github_repo_confidence(repo: dict[str, Any]) -> str:
    stars = int(repo.get("stars") or 0)
    forks = int(repo.get("forks") or 0)
    if stars >= 1000 or forks >= 100:
        return "high"
    if stars >= 50 or forks >= 10:
        return "medium"
    return "low"


def _external_result_content(result: dict[str, Any]) -> str:
    title = str(result.get("title") or "").strip()
    summary = str(result.get("summary") or "").strip()
    text = str(result.get("text") or "").strip()
    highlights = " ".join(str(item) for item in _list(result.get("highlights")) if str(item).strip())
    parts = [part for part in (title, summary, highlights, text) if part]
    return " ".join(" ".join(part.split()) for part in parts).strip()


def _external_result_confidence(result: dict[str, Any]) -> str:
    try:
        score = float(result.get("score") or 0)
    except (TypeError, ValueError):
        score = 0
    if score >= 0.75:
        return "high"
    if score <= 0.1:
        return "low"
    return "medium"


def _exa_diagnostic_records(*, index: int, source: str, diagnostics: Any) -> list[EvidenceRecord]:
    if not isinstance(diagnostics, dict):
        return []
    records: list[EvidenceRecord] = []
    intent_results = diagnostics.get("intent_results") if isinstance(diagnostics.get("intent_results"), dict) else {}
    for status_name, intents in (
        ("failed", diagnostics.get("failed_intents") or []),
        ("no_results", diagnostics.get("no_result_intents") or []),
    ):
        for intent_index, intent_value in enumerate(_list(intents)):
            intent = str(intent_value or "").strip()
            if not intent:
                continue
            details = intent_results.get(intent) if isinstance(intent_results, dict) else {}
            query = str(details.get("query") or "") if isinstance(details, dict) else ""
            error = str(details.get("error") or "") if isinstance(details, dict) else ""
            content = f"Exa {status_name} for external proof intent '{intent}'."
            if query:
                content += f" Query: {query}."
            if error:
                content += f" Error: {error}."
            records.append(
                EvidenceRecord(
                    ref=f"raw_inputs.{index}.exa.diagnostics.{status_name}.{intent_index}",
                    source=source,
                    evidence_type=f"acquisition.{status_name}",
                    content=content[:_RAW_INPUT_CONTENT_CHARS],
                    confidence="low",
                    metadata={
                        "source_class": "acquisition_metadata",
                        "provider": "exa",
                        "intent": intent,
                        "status": status_name,
                    },
                )
            )
    return records


def _evidence_from_web_payload(*, index: int, source: str, payload: dict[str, Any]) -> list[EvidenceRecord]:
    text = _summarize_payload(payload, limit=None)
    if not text:
        return []
    url = first_string(payload.get("url"), payload.get("source_url"), payload.get("page_url"))
    homepage, subpages, missing_subpages = _split_web_subpages(text)
    subpages = _strip_cross_page_boilerplate(homepage, subpages)
    records: list[EvidenceRecord] = []
    if homepage:
        records.append(_raw_input_record(index=index, source=source, content=homepage[:_RAW_INPUT_CONTENT_CHARS], url=url))
    owned_urls = [candidate for candidate in [url, *[subpage_url for subpage_url, _ in subpages]] if candidate]
    strategic_surface_found = any(_is_strategic_surface(candidate) for candidate in owned_urls)
    for subpage_index, (subpage_url, subpage_text) in enumerate(subpages, start=1):
        for chunk_index, chunk in enumerate(_chunk_text_by_section(subpage_text), start=1):
            records.append(
                _raw_input_record(
                    index=index,
                    source=source,
                    content=chunk,
                    url=subpage_url or url,
                    ref=f"raw_inputs.{index}.subpage.{subpage_index}.chunk.{chunk_index}",
                    metadata={"subpage_url": subpage_url},
                )
            )
        records.extend(
            _absence_records_for_owned_page(
                index=index,
                source=source,
                subpage_index=subpage_index,
                url=subpage_url or url,
                text=subpage_text,
            )
        )
    if owned_urls and not strategic_surface_found:
        records.append(
            _strategic_surfaces_none_found_record(
                index=index,
                source=source,
                crawled_url_count=len(dict.fromkeys(owned_urls)),
            )
        )
    for subpage_index, (subpage_url, _) in missing_subpages:
        records.append(
            _acquisition_attempt_record(
                ref=f"raw_inputs.{index}.subpage.{subpage_index}.diagnostics.not_found",
                source=source,
                provider="web",
                intent="owned_surface",
                status="not_found",
                query=subpage_url,
                error="Captured page looked like a 404/not found response.",
                url=subpage_url or url,
            )
        )
    return records


def _strategic_surfaces_none_found_record(*, index: int, source: str, crawled_url_count: int) -> EvidenceRecord:
    return EvidenceRecord(
        ref=f"raw_inputs.{index}.diagnostics.strategic_surfaces",
        source=source,
        evidence_type="acquisition.attempt.strategic_surfaces",
        content=(
            "Owned web capture did not find any strategic about/culture/values/"
            "manifesto surfaces among crawled URLs."
        )[:_RAW_INPUT_CONTENT_CHARS],
        confidence="low",
        metadata={
            "source_class": "acquisition_metadata",
            "provider": "web",
            "intent": "strategic_surfaces",
            "status": "none_found",
            "crawled_url_count": crawled_url_count,
        },
    )


def _raw_input_record(
    *,
    index: int,
    source: str,
    content: str,
    url: str = "",
    ref: str = "",
    metadata: dict[str, Any] | None = None,
) -> EvidenceRecord:
    record_ref = ref or f"raw_inputs.{index}"
    trimmed = str(content or "").strip()[:_RAW_INPUT_CONTENT_CHARS]
    record_metadata = dict(metadata or {})
    record_metadata["source_class"] = classify_source(
        ref=record_ref,
        source=source,
        evidence_type="raw_input",
        content=trimmed,
    )
    return EvidenceRecord(
        ref=record_ref,
        source=source,
        evidence_type="raw_input",
        content=trimmed,
        url=url,
        confidence="medium",
        metadata=record_metadata,
    )


def _evidence_from_features(features: list[Any]) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    dropped_derived_strategy = 0
    for index, row in enumerate(features):
        feature = row if isinstance(row, dict) else {}
        feature_name = str(feature.get("feature_name") or "")
        dimension_name = str(feature.get("dimension_name") or "")
        if not feature_name and not dimension_name:
            continue
        value = feature.get("raw_value") or feature.get("value")
        content = str(value or "").strip()
        if not content:
            continue
        ref = f"features.{index}"
        evidence_type = f"{dimension_name}.{feature_name}".strip(".")
        source_class = classify_source(
            ref=ref,
            source="legacy_feature",
            evidence_type=evidence_type,
            content=content[:700],
        )
        if source_class == SOURCE_CLASS_DERIVED_STRATEGY:
            dropped_derived_strategy += 1
            continue
        records.append(
            EvidenceRecord(
                ref=ref,
                source="legacy_feature",
                evidence_type=evidence_type,
                content=content[:700],
                confidence=feature_confidence(feature.get("confidence")),
                metadata={"source_class": source_class},
            )
        )
    if dropped_derived_strategy:
        records.append(
            EvidenceRecord(
                ref="features.dropped_derived_strategy",
                source="legacy_feature",
                evidence_type="acquisition.dropped_derived_strategy",
                content=f"Dropped {dropped_derived_strategy} legacy derived-strategy feature(s) from SV9 Flow evidence.",
                confidence="low",
                metadata={
                    "source_class": "acquisition_metadata",
                    "dropped_count": dropped_derived_strategy,
                },
            )
        )
    return records


def _evidence_from_visual_signature(evidence: dict[str, Any] | None) -> list[EvidenceRecord]:
    if not isinstance(evidence, dict):
        return []
    if evidence.get("schema_version") != "visual-signature-evidence-v1":
        return []
    records: list[EvidenceRecord] = []
    capture = evidence.get("capture") if isinstance(evidence.get("capture"), dict) else {}
    if capture.get("status") != "usable":
        return []
    records.append(
        EvidenceRecord(
            ref="visual_signature.capture",
            source="visual_signature",
            evidence_type="visual_capture",
            content=f"capture_status={capture.get('status')}; first_fold_evaluable={capture.get('first_fold_evaluable')}",
            confidence="medium",
            metadata={"capture": capture, "source_class": "visual_signal"},
        )
    )
    for index, tile_signal in enumerate(evidence.get("tile_signals") or []):
        if not isinstance(tile_signal, dict):
            continue
        records.append(
            EvidenceRecord(
                ref=f"visual_signature.tile_signals.{index}",
                source="visual_signature",
                evidence_type="visual_tile_signal",
                content=str(tile_signal.get("rationale") or tile_signal.get("effect") or "")[:700],
                confidence=feature_confidence(tile_signal.get("confidence")),
                metadata={
                    "source_class": "visual_signal",
                    "tile": tile_signal.get("tile"),
                    "effect": tile_signal.get("effect"),
                    "source": tile_signal.get("source"),
                },
            )
        )
    return records


def _payload_dict(entry: dict[str, Any]) -> dict[str, Any]:
    payload = entry.get("payload")
    if isinstance(payload, dict):
        return payload
    payload_json = entry.get("payload_json")
    if isinstance(payload_json, str):
        try:
            parsed = json.loads(payload_json)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _strip_cross_page_boilerplate(
    homepage: str,
    subpages: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Drop lines repeated across several captured pages from subpage text.

    Captures ingest shared chrome (nav menus, footers) on every page; those
    lines are template, not evidence, and their keyword noise outranks real
    strategy copy in block shortlists. The homepage keeps one copy untouched.
    """

    pages = [homepage] + [text for _, text in subpages]
    if len(pages) < _BOILERPLATE_MIN_PAGES:
        return subpages
    counts: dict[str, int] = {}
    for page in pages:
        for line in {_normalized_line(raw) for raw in page.splitlines()}:
            if line:
                counts[line] = counts.get(line, 0) + 1
    boilerplate = {line for line, count in counts.items() if count >= _BOILERPLATE_MIN_PAGES}
    if not boilerplate:
        return subpages
    cleaned: list[tuple[str, str]] = []
    for subpage_url, text in subpages:
        kept = [raw for raw in text.splitlines() if _normalized_line(raw) not in boilerplate]
        cleaned_text = "\n".join(kept).strip()
        if cleaned_text:
            cleaned.append((subpage_url, cleaned_text))
    return cleaned


def _normalized_line(line: str) -> str:
    return " ".join(line.split()).lower()


def _split_web_subpages(text: str) -> tuple[str, list[tuple[str, str]], list[tuple[int, tuple[str, str]]]]:
    marker = "\n---\n## Subpage: "
    if marker not in text:
        return text.strip(), [], []
    first, *raw_subpages = text.split(marker)
    subpages: list[tuple[str, str]] = []
    missing_subpages: list[tuple[int, tuple[str, str]]] = []
    for subpage_index, raw_subpage in enumerate(raw_subpages, start=1):
        lines = raw_subpage.splitlines()
        if not lines:
            continue
        subpage_url = lines[0].strip()
        subpage_text = "\n".join(lines[1:]).strip()
        if not subpage_text:
            continue
        if _is_not_found_page(subpage_text):
            missing_subpages.append((subpage_index, (subpage_url, subpage_text)))
        else:
            subpages.append((subpage_url, subpage_text))
    return first.strip(), subpages, missing_subpages


def _is_not_found_page(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    first_slice = normalized[:240]
    return (
        "404 not found" in first_slice
        or first_slice.startswith("not found the requested url was not found")
        or "the requested url was not found on this server" in first_slice
    )


def _chunk_text_by_section(text: str) -> list[str]:
    clean = str(text or "").strip()
    if not clean:
        return []
    chunks: list[str] = []
    for section in _markdown_sections(clean):
        if len(section) <= _WEB_CHUNK_CHARS:
            chunks.append(section)
        else:
            chunks.extend(_fixed_size_chunks(section))
        if len(chunks) >= _MAX_WEB_SUBPAGE_CHUNKS:
            break
    return chunks[:_MAX_WEB_SUBPAGE_CHUNKS]


def _fixed_size_chunks(text: str) -> list[str]:
    chunks: list[str] = []
    step = max(1, _WEB_CHUNK_CHARS - _WEB_CHUNK_OVERLAP)
    for start in range(0, len(text), step):
        chunk = text[start : start + _WEB_CHUNK_CHARS].strip()
        if chunk:
            chunks.append(chunk)
        if len(chunks) >= _MAX_WEB_SUBPAGE_CHUNKS or start + _WEB_CHUNK_CHARS >= len(text):
            break
    return chunks


def _markdown_sections(text: str) -> list[str]:
    sections: list[list[str]] = []
    current: list[str] = []
    for line in str(text or "").splitlines():
        if re.match(r"^\s{0,3}#{1,6}\s+\S", line) and current:
            sections.append(current)
            current = [line]
            continue
        current.append(line)
    if current:
        sections.append(current)
    cleaned = ["\n".join(section).strip() for section in sections]
    return [section for section in cleaned if section]


def _absence_records_for_owned_page(
    *,
    index: int,
    source: str,
    subpage_index: int,
    url: str,
    text: str,
) -> list[EvidenceRecord]:
    if not _is_strategic_surface(url):
        return []
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return []
    records: list[EvidenceRecord] = []
    for block, terms in _ABSENCE_BLOCK_TERMS.items():
        if any(term in normalized for term in terms):
            continue
        records.append(
            EvidenceRecord(
                ref=f"raw_inputs.{index}.subpage.{subpage_index}.absence.{block}",
                source=source,
                evidence_type=f"acquisition.absence.{block}",
                content=(
                    f"Crawled owned page {url or '(unknown URL)'}; no explicit {block} "
                    "section terms were observed in the captured text."
                )[:_RAW_INPUT_CONTENT_CHARS],
                url=url,
                confidence="low",
                metadata={
                    "source_class": "acquisition_metadata",
                    "checked_block": block,
                    "checked_url": url,
                },
            )
        )
    return records


def _is_strategic_surface(url: str) -> bool:
    value = str(url or "").lower()
    return any(marker in value for marker in _ABSENCE_SURFACE_MARKERS)


def _acquisition_attempt_record(
    *,
    ref: str,
    source: str,
    provider: str,
    intent: str,
    status: str,
    query: str = "",
    error: str = "",
    url: str | None = None,
) -> EvidenceRecord:
    status_text = status or "unknown"
    content = f"{provider} acquisition attempt for '{intent}' ended with status '{status_text}'."
    if query:
        content += f" Query: {query}."
    if error:
        content += f" Error: {error}."
    return EvidenceRecord(
        ref=ref,
        source=source,
        evidence_type=f"acquisition.attempt.{intent}",
        content=content[:_RAW_INPUT_CONTENT_CHARS],
        url=url,
        confidence="low",
        metadata={
            "source_class": "acquisition_metadata",
            "provider": provider,
            "intent": intent,
            "status": status_text,
            "query": query,
            "error": error,
        },
    )


def _summarize_payload(payload: dict[str, Any], *, limit: int | None = _RAW_INPUT_CONTENT_CHARS) -> str:
    for key in ("text", "content", "markdown", "markdown_content", "summary", "title"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            return text if limit is None else text[:limit]
    if payload:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return text if limit is None else text[:limit]
    return ""


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
