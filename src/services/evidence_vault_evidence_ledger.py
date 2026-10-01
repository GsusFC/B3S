"""Shadow ledger of whether a re-scan saw, disproved or could not check prior evidence.

Every prior evidence identity gets exactly one state against the current capture.
``not_verified`` is the fail-stable default: absence is claimed only when an owned
page was re-captured healthily and the prior chunk's own phrases are gone from it.
Rows are diagnostics and never change a score.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from fractions import Fraction
import hashlib
import re
from types import MappingProxyType
from typing import Any
import unicodedata
from urllib.parse import urlsplit

from src.evidence_identity import normalize_evidence_url
from src.services.evidence_memory_identity_v2 import project_evidence_memory_row_identity
from src.services.evidence_vault_sv9_support_continuity import MIN_PAGE_SHINGLES, meets_similarity_threshold
from src.sv9.flow_ingress import _MAX_EVIDENCE_CHARS, detection_blocks_from_flow_candidate, flow_candidate_extra_signals
from src.sv9.rubric import COMPONENTS, PRESENTATION_ORDER
from src.sv9_flow.contracts import (
    SV9_FLOW_CANDIDATE_VERSION,
    BrandEvidencePack,
    BrandInterpretation,
    EvidenceRecord,
    Sv9FlowCandidate,
    TileSignal,
)


EVIDENCE_LEDGER_POLICY_VERSION = "evidence-vault-evidence-ledger-v3"
SEEN, VERIFIED_ABSENT, NOT_VERIFIED = "seen", "verified_absent", "not_verified"
SHOWN, SHOWN_AS_SIGNAL, NOT_SHOWN = "shown", "shown_as_signal", "not_shown"
OWNED_PAGE_CLASS, EXTERNAL_CLASS = "owned_page", "external"
# Provisional current/prior page-size floor; calibrate it with scripts/evidence_ledger_measure.py.
MIN_PAGE_SIZE_RATIO = Fraction(1, 2)
# Share of a prior chunk's 3-word shingles still on its current page. Whole-chunk matching
# turned a changed CDN image URL, a weather widget or video-player text into absences.
SEEN_CONTAINMENT = Fraction(9, 10)
# Absence is measured on the chunk's own shingles, those found once in the whole prior owned
# capture: phrases repeated elsewhere (nav, widgets, duplicated blocks) survive a real removal.
ABSENT_CONTAINMENT = Fraction(1, 5)
# One edited word touches at most 3 shingles, so 5 own shingles keep an edit from faking absence.
MIN_OWN_SHINGLES = 5
# Anti-bot interstitials captured as page text: a page carrying one proves no absence.
_BOT_CHALLENGE_MARKERS = (
    "challenges.cloudflare.com/cdn-cgi/challenge-platform",
    "cloudflare.com/products/turnstile",
    "checking your browser",
    "verify you are human",
)
_HEALTHY_GATE_STATES = frozenset({"pass", "warning"})
# An allow-list: the acquisition gate lets an obstructed (cookie-wall) web capture pass.
_WEB_SUCCESS_STATUSES = frozenset({"fetched", "hit", "success"})
# Search results, even on brand URLs: a query that stops returning them proves nothing.
_EXTERNAL_SOURCES = frozenset({"exa", "searchapi", "github"})
# The SV9 Flow evidence worker splits owned captures on this marker and removes lines
# repeated on this many pages from subpages before chunking them into evidence.
_SUBPAGE_MARKER = "\n---\n## Subpage: "
_BOILERPLATE_MIN_PAGES = 3
_PAYLOAD_TEXT_KEYS = ("text", "content", "markdown", "markdown_content", "summary", "title")
_NOT_FOUND_PREFIX_CHARS = 240
_SHINGLE_WORDS = 3
_WORD = re.compile(r"\w+")
_ROW_IDENTITY_KEYS = ("evidence_ref", "evidence_fingerprint", "evidence_id", "source_identity_id", "source_key")

Verifier = Callable[[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], tuple[str, list[str]]]


def source_key(url: Any) -> str:
    """Same-source key: tracking keys, fragment and trailing slash dropped; scheme and ``www.`` aliased."""

    normalized = normalize_evidence_url(url)
    parts = urlsplit(normalized)
    if not parts.netloc:
        return normalized
    key = parts.netloc.removeprefix("www.") + parts.path
    return f"{key}?{parts.query}" if parts.query else key


def classify_evidence(
    prior_row: Mapping[str, Any], current_capture: Mapping[str, Any], shown_index: Mapping[str, Any]
) -> tuple[str, list[str]]:
    """Dispatch one prior row to its class verifier; an unregistered class stays not_verified."""

    verifier = EVIDENCE_VERIFIERS.get(str(prior_row.get("evidence_class") or ""))
    if verifier is None:
        return NOT_VERIFIED, ["unknown_evidence_class"]
    return verifier(prior_row, current_capture, shown_index)


def build_evidence_ledger(
    *,
    brand_domain: str,
    prior_snapshot: Mapping[str, Any],
    prior_rows: Sequence[Mapping[str, Any]],
    current_snapshot: Mapping[str, Any],
    current_rows: Sequence[Mapping[str, Any]],
    shown_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify every prior evidence identity against the current capture.

    Snapshots are frozen capture payloads; rows use the capture evidence shape
    (ref, source, evidence_type, url, content, metadata). Rows without a material
    evidence identity are counted in ``skipped_rows`` instead of classified.
    """

    prior = _capture_view(prior_snapshot, prior_rows, brand_domain)
    current = _capture_view(current_snapshot, current_rows, brand_domain)
    _strip_boilerplate(current["pages"], prior["boilerplate"])
    index = build_shown_index([]) if shown_index is None else shown_index
    capture_counts = Counter(item for page in prior["pages"].values() for item in _shingle_list(page["text"]))
    rows = []
    for evidence in prior["evidence"]:
        prior_page = prior["pages"].get(evidence["source_key"])
        own = {item for item in _shingles(evidence["content"]) if capture_counts[item] == 1}
        state, reasons = classify_evidence({**evidence, "prior_page": prior_page, "own_shingles": own}, current, index)
        rows.append(_ledger_row(evidence, state, reasons, prior_page, current, index, own))
    rows.sort(key=lambda row: (row["evidence_ref"], row["evidence_fingerprint"]))
    return {
        "policy_version": EVIDENCE_LEDGER_POLICY_VERSION,
        "runtime_effect": False,
        "rows": rows,
        "skipped_rows": dict(sorted(prior["skipped"].items())),
    }


def build_shown_index(evaluations: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index, per SV9 component, the evidence Core's prompt showed in one scan.

    Each evaluation is ``{"component_key", "status", "candidate", "component_result"}``
    where the candidate is a stored Flow candidate dict and the component result is
    Core's stored result, whose ``evidence`` is what its prompt showed. Without a
    result, the candidate's detection block stands in for it. Components absent
    from the input were not evaluated in that scan.
    """

    index = {key: _unindexed("component_not_evaluated") for key in PRESENTATION_ORDER}
    for evaluation in evaluations:
        key = str(evaluation.get("component_key") or "")
        if key in index:
            index[key] = _component_entry(key, evaluation)
    return index


def shown_to_core(fragment: Any, shown_index: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Whether Core's prompt showed this fragment, per component; it never feeds the ledger state."""

    needle = _normalized(fragment)
    return {key: _shown_status(needle, entry) for key, entry in shown_index.items()}


def summarize_tile_support(
    ledger_rows: Iterable[Mapping[str, Any]], tile_judgments: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Ledger state of each tile's supporting (ref, fingerprint) pairs."""

    by_pair = {(row["evidence_ref"], row["evidence_fingerprint"]): row for row in ledger_rows}
    summaries = []
    for judgment in tile_judgments:
        evidence = [_support_state(item, by_pair) for item in judgment.get("supporting_evidence") or []]
        summaries.append(
            {
                "tile_id": str(judgment["tile_id"]),
                "supporting_evidence": evidence,
                "state_counts": dict(sorted(Counter(item["state"] for item in evidence).items())),
            }
        )
    return summaries


def compare_owned_pages(prior_snapshot: Mapping[str, Any], current_snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Label every owned page ``unchanged`` (3-word-shingle Jaccard >= 0.80), ``changed`` or ``unknown``."""

    prior, current = _pages_view(prior_snapshot)["pages"], _pages_view(current_snapshot)["pages"]
    return [
        {
            "source_key": key,
            "in_prior": bool(prior.get(key, {}).get("normalized")),
            "in_current": bool(current.get(key, {}).get("normalized")),
            "change": _page_change(prior.get(key), current.get(key)),
            "prior_lastmod": prior.get(key, {}).get("lastmod", ""),
            "current_lastmod": current.get(key, {}).get("lastmod", ""),
        }
        for key in sorted(set(prior) | set(current))
    ]


def _verify_owned_page(
    prior_row: Mapping[str, Any], current_capture: Mapping[str, Any], _shown_index: Mapping[str, Any]
) -> tuple[str, list[str]]:
    page = current_capture["pages"].get(prior_row["source_key"])
    if page is not None and prior_row["normalized"] and (
        prior_row["normalized"] in page["normalized"] or prior_row["normalized"] in page["stripped"]
    ):
        return SEEN, []
    containment = _chunk_containment(prior_row, page)
    if containment is not None and containment >= SEEN_CONTAINMENT:
        return SEEN, []
    reasons = _capture_reasons(current_capture) + _page_reasons(page, prior_row["prior_page"])
    if reasons:
        return NOT_VERIFIED, reasons
    if containment is None:
        return NOT_VERIFIED, ["chunk_too_short"]
    own = prior_row.get("own_shingles") or set()
    if len(own) < MIN_OWN_SHINGLES:
        return NOT_VERIFIED, ["chunk_not_distinctive"]
    if _own_containment(own, page) > ABSENT_CONTAINMENT:
        return NOT_VERIFIED, ["chunk_text_changed"]
    return VERIFIED_ABSENT, []


def _verify_external(
    prior_row: Mapping[str, Any], current_capture: Mapping[str, Any], _shown_index: Mapping[str, Any]
) -> tuple[str, list[str]]:
    if any(_same_external_item(prior_row, row) for row in current_capture["evidence"]):
        return SEEN, []
    return NOT_VERIFIED, ["external_exact_fetch_unavailable"]


EVIDENCE_VERIFIERS: Mapping[str, Verifier] = MappingProxyType(
    {OWNED_PAGE_CLASS: _verify_owned_page, EXTERNAL_CLASS: _verify_external}
)


def _same_external_item(prior_row: Mapping[str, Any], row: Mapping[str, Any]) -> bool:
    if row["evidence_class"] != EXTERNAL_CLASS:
        return False
    if row["evidence_id"] == prior_row["evidence_id"]:
        return True
    return bool(
        prior_row["source_key"]
        and row["source_key"] == prior_row["source_key"]
        and prior_row["normalized"]
        and prior_row["normalized"] in row["normalized"]
    )


def _capture_reasons(capture: Mapping[str, Any]) -> list[str]:
    reasons = []
    if capture["gate_state"] not in _HEALTHY_GATE_STATES:
        reasons.append("capture_blocked")
    if capture["web_step_status"] not in _WEB_SUCCESS_STATUSES:
        reasons.append("capture_failed")
    return reasons


def _page_reasons(page: Mapping[str, Any] | None, prior_page: Mapping[str, Any] | None) -> list[str]:
    if page is None or page["visit_status"] is None:
        return ["page_not_visited"]
    if page["visit_status"] != "captured" or not page["normalized"]:
        return ["page_not_captured"]
    reasons = ["page_404"] if _looks_not_found(page["text"]) else []
    if _looks_obstructed(page["normalized"]):
        reasons.append("page_obstructed")
    if prior_page is None or not prior_page["normalized"]:
        return [*reasons, "prior_page_unavailable"]
    if _looks_obstructed(prior_page["normalized"]):
        reasons.append("prior_page_obstructed")
    if min(len(prior_page["shingles"]), len(page["shingles"])) < MIN_PAGE_SHINGLES:
        reasons.append("page_too_short")
    if len(page["normalized"]) < len(prior_page["normalized"]) * MIN_PAGE_SIZE_RATIO:
        reasons.append("page_truncated")
    return reasons


def _page_change(prior_page: Mapping[str, Any] | None, current_page: Mapping[str, Any] | None) -> str:
    if prior_page is None or current_page is None:
        return "unknown"
    prior, current = prior_page["shingles"], current_page["shingles"]
    if min(len(prior), len(current)) < MIN_PAGE_SHINGLES:
        return "unknown"
    return "unchanged" if meets_similarity_threshold(len(prior & current), len(prior | current)) else "changed"


def _ledger_row(
    evidence: Mapping[str, Any],
    state: str,
    reasons: list[str],
    prior_page: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    shown_index: Mapping[str, Any],
    own: set[str],
) -> dict[str, Any]:
    page = current["pages"].get(evidence["source_key"])
    owned = evidence["evidence_class"] == OWNED_PAGE_CLASS
    return {
        **{key: evidence[key] for key in _ROW_IDENTITY_KEYS},
        "evidence_class": evidence["evidence_class"],
        "state": state,
        "reason_codes": reasons,
        "health": {
            "capture_gate_state": current["gate_state"],
            "web_step_status": current["web_step_status"],
            "page": _page_health(prior_page, page),
            "chunk_containment": _containment_health(evidence, page),
            "own_shingles": len(own) if owned else None,
            "own_containment": _rounded(_own_containment(own, page)) if owned and own and page is not None else None,
        },
        "shown_to_core": shown_to_core(evidence["content"], shown_index),
        "policy_version": EVIDENCE_LEDGER_POLICY_VERSION,
        "runtime_effect": False,
    }


def _page_health(prior_page: Mapping[str, Any] | None, current_page: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if prior_page is None and current_page is None:
        return None
    return {
        "visit_status": current_page["visit_status"] if current_page else None,
        "prior_chars": len(prior_page["normalized"]) if prior_page else None,
        "current_chars": len(current_page["normalized"]) if current_page else None,
        "change": _page_change(prior_page, current_page),
    }


def _chunk_containment(row: Mapping[str, Any], page: Mapping[str, Any] | None) -> Fraction | None:
    chunk = _shingles(row["content"])
    if page is None or not chunk:
        return None
    return Fraction(len(chunk & page["shingles"]), len(chunk))


def _own_containment(own: set[str], page: Mapping[str, Any]) -> Fraction:
    return Fraction(len(own & page["shingles"]), len(own))


def _containment_health(evidence: Mapping[str, Any], page: Mapping[str, Any] | None) -> float | None:
    if evidence["evidence_class"] != OWNED_PAGE_CLASS:
        return None
    return _rounded(_chunk_containment(evidence, page))


def _rounded(value: Fraction | None) -> float | None:
    return None if value is None else round(float(value), 3)


def _capture_view(snapshot: Mapping[str, Any], rows: Sequence[Mapping[str, Any]], brand_domain: str) -> dict[str, Any]:
    view = _pages_view(snapshot)
    evidence, skipped = [], Counter()
    for raw in rows:
        row = _evidence_row(raw, brand_domain, view["aliases"])
        if row is None:
            metadata = raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {}
            skipped[str(metadata.get("source_class") or "unknown")] += 1
        else:
            evidence.append(row)
    return {
        **view,
        "gate_state": _gate_state(snapshot),
        "web_step_status": _web_step_status(snapshot),
        "evidence": evidence,
        "skipped": skipped,
    }


def _evidence_row(raw: Mapping[str, Any], brand_domain: str, aliases: Mapping[str, str]) -> dict[str, Any] | None:
    identity = project_evidence_memory_row_identity(raw, brand_domain=brand_domain)
    if identity is None:
        return None
    content = str(raw.get("content") or "")
    return {
        "evidence_ref": str(raw.get("ref") or ""),
        "evidence_fingerprint": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "evidence_id": identity["evidence_id"],
        "source_identity_id": identity["document_id"],
        "source_key": _page_key(raw.get("url"), aliases),
        "evidence_class": _evidence_class(identity),
        "content": content,
        "normalized": _normalized(content),
    }


def _evidence_class(identity: Mapping[str, Any]) -> str:
    source, source_class = identity["source"], identity["source_class"]
    if source in _EXTERNAL_SOURCES or source_class == "external_proof":
        return EXTERNAL_CLASS
    if source == "web" and source_class == "owned_copy":
        return OWNED_PAGE_CLASS
    return source


def _gate_state(snapshot: Mapping[str, Any]) -> str:
    gate = snapshot.get("acquisition_gate")
    state = gate.get("state") if isinstance(gate, Mapping) else None
    return str(state or "unknown").strip().lower()


def _web_step_status(snapshot: Mapping[str, Any]) -> str:
    steps = snapshot.get("acquisition_steps")
    step = steps.get("web") if isinstance(steps, Mapping) else None
    status = step.get("status") if isinstance(step, Mapping) else None
    return str(status or "missing").strip().lower()


def _pages_view(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Owned pages by source key; the homepage is keyed by its final URL, the requested URL is an alias."""

    pages: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    boilerplate: set[str] = set()
    for payload in _web_payloads(snapshot):
        provenance = payload.get("capture_provenance") if isinstance(payload.get("capture_provenance"), Mapping) else {}
        home = source_key(provenance.get("final_url")) or source_key(payload.get("url"))
        if not home:
            continue
        for alias in (source_key(payload.get("url")), source_key(provenance.get("final_url"))):
            if alias:
                aliases[alias] = home
        homepage, subpages = _split_pages(_payload_text(payload))
        boilerplate |= _boilerplate_lines([homepage, *(text for _url, text in subpages if not _looks_not_found(text))])
        _add_text(pages, home, homepage)
        for url, text in subpages:
            _add_text(pages, _page_key(url, aliases), text)
        selection = payload.get("page_selection") if isinstance(payload.get("page_selection"), Mapping) else {}
        for row in _mappings(selection.get("visited_pages")):
            _add_selection_row(pages, _page_key(row.get("url"), aliases), row, visited=True)
        for row in _mappings(selection.get("known_pages")):
            _add_selection_row(pages, _page_key(row.get("url"), aliases), row, visited=False)
    for page in pages.values():
        page["normalized"] = page["stripped"] = _normalized(page["text"])
        page["shingles"] = _shingles(page["text"])
    return {"pages": pages, "aliases": aliases, "boilerplate": boilerplate}


def _web_payloads(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        entry["payload"]
        for entry in _mappings(snapshot.get("raw_inputs"))
        if entry.get("source") == "web" and isinstance(entry.get("payload"), Mapping)
    ]


def _payload_text(payload: Mapping[str, Any]) -> str:
    for key in _PAYLOAD_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _split_pages(text: str) -> tuple[str, list[tuple[str, str]]]:
    first, *raw_subpages = text.split(_SUBPAGE_MARKER)
    subpages = []
    for raw in raw_subpages:
        lines = raw.splitlines()
        body = "\n".join(lines[1:]).strip()
        if lines and body:
            subpages.append((lines[0].strip(), body))
    return first.strip(), subpages


def _boilerplate_lines(pages: list[str]) -> set[str]:
    if len(pages) < _BOILERPLATE_MIN_PAGES:
        return set()
    counts = Counter(line for page in pages for line in {_line_key(raw) for raw in page.splitlines()} if line)
    return {line for line, count in counts.items() if count >= _BOILERPLATE_MIN_PAGES}


def _strip_boilerplate(pages: Mapping[str, dict[str, Any]], boilerplate: set[str]) -> None:
    """Also search current pages without the prior capture's boilerplate lines.

    Prior subpage evidence was chunked after those lines were removed, so a prior
    fragment can join text that is still split by a boilerplate line today.
    """

    if not boilerplate:
        return
    for page in pages.values():
        kept = (line for line in page["text"].splitlines() if _line_key(line) not in boilerplate)
        page["stripped"] = _normalized("\n".join(kept))


def _add_text(pages: dict[str, dict[str, Any]], key: str, text: str) -> None:
    if not key or not text:
        return
    page = pages.setdefault(key, _empty_page())
    page["text"] = f"{page['text']}\n{text}" if page["text"] else text


def _add_selection_row(pages: dict[str, dict[str, Any]], key: str, row: Mapping[str, Any], *, visited: bool) -> None:
    if not key:
        return
    page = pages.setdefault(key, _empty_page())
    if visited and page["visit_status"] != "captured":
        page["visit_status"] = str(row.get("status") or "unknown").strip().lower()
    page["lastmod"] = page["lastmod"] or str(row.get("lastmod") or "")


def _empty_page() -> dict[str, Any]:
    return {"text": "", "visit_status": None, "lastmod": ""}


def _page_key(url: Any, aliases: Mapping[str, str]) -> str:
    key = source_key(url)
    return aliases.get(key, key)


def _looks_not_found(text: str) -> bool:
    """The SV9 Flow evidence worker's 404 text rule."""

    normalized = " ".join(str(text or "").strip().lower().split())
    first = normalized[:_NOT_FOUND_PREFIX_CHARS]
    return bool(first) and (
        "404 not found" in first
        or first.startswith("not found the requested url was not found")
        or "the requested url was not found on this server" in first
    )


def _looks_obstructed(normalized: str) -> bool:
    return any(marker in normalized for marker in _BOT_CHALLENGE_MARKERS)


def _component_entry(key: str, evaluation: Mapping[str, Any]) -> dict[str, Any]:
    status = str(evaluation.get("status") or "")
    if status == "not_detected":
        return _unindexed("component_not_detected")
    if status != "evaluated":
        return _unindexed("component_not_evaluated")
    try:
        candidate = _flow_candidate(evaluation.get("candidate"))
        blocks = detection_blocks_from_flow_candidate(candidate)
        signals = flow_candidate_extra_signals(candidate).get(key, [])
    except (AttributeError, KeyError, TypeError, ValueError):
        return _unindexed("candidate_unavailable")
    contents = {record.ref: " ".join(str(record.content or "").split()) for record in candidate.evidence_pack.evidence}
    tldr_key = COMPONENTS[key]["tldr_key"]
    block = blocks.get(tldr_key) if tldr_key else None
    if tldr_key and evaluation.get("component_result") is not None:
        block = _core_prompt_block(evaluation["component_result"], contents)
    return {
        # Coherencia has no detection block; the evaluator assembles its quotes privately.
        "reason_codes": [] if tldr_key else ["component_evidence_not_indexed"],
        "snippets": _snippet_refs(block, contents),
        "signals": {
            ref: contents[ref].casefold()
            for signal in signals
            for ref in signal.get("source_evidence_refs") or []
            if ref in contents
        },
    }


def _core_prompt_block(result: Mapping[str, Any], contents: Mapping[str, str]) -> dict[str, Any]:
    """Core's own prompt evidence, bound to each ref whose Flow ingress snippet it is.

    A ref whose content only starts with a received snippet stays unbound: Core never saw the rest.
    """

    evidence = list(result["evidence"])
    refs = [ref for ref, content in contents.items() if content and content[:_MAX_EVIDENCE_CHARS] in evidence]
    return {"evidence": evidence, "evaluation_evidence_refs": refs}


def _snippet_refs(block: Any, contents: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Refs whose collapsed content starts with a snippet Core received, and how much of it Core saw."""

    if not isinstance(block, Mapping):
        return {}
    snippets = [str(item) for item in block.get("evidence") or [] if str(item or "")]
    indexed = {}
    for ref in block.get("evaluation_evidence_refs") or []:
        content = contents.get(ref) or ""
        shown_chars = max((len(snippet) for snippet in snippets if content.startswith(snippet)), default=0)
        if shown_chars:
            indexed[ref] = {"content": content.casefold(), "shown_chars": shown_chars}
    return indexed


def _unindexed(reason: str) -> dict[str, Any]:
    return {"reason_codes": [reason], "snippets": {}, "signals": {}}


def _shown_status(needle: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    snippets = entry["snippets"]
    shown = sorted(
        ref for ref, item in snippets.items() if needle and 0 <= item["content"].find(needle) < item["shown_chars"]
    )
    if shown:
        return _shown(SHOWN, [], shown)
    signals = sorted(ref for ref, content in entry["signals"].items() if needle and needle in content)
    if signals:
        return _shown(SHOWN_AS_SIGNAL, [], signals)
    if entry["reason_codes"]:
        return _shown(NOT_SHOWN, list(entry["reason_codes"]), [])
    beyond = any(needle and needle in item["content"] for item in snippets.values())
    return _shown(NOT_SHOWN, ["fragment_after_snippet_limit" if beyond else "not_in_evaluation_evidence"], [])


def _shown(status: str, reasons: list[str], refs: list[str]) -> dict[str, Any]:
    return {"status": status, "reason_codes": reasons, "evidence_refs": refs}


def _flow_candidate(value: Any) -> Sv9FlowCandidate:
    if not isinstance(value, Mapping):
        raise TypeError("flow candidate must be an object")
    pack = dict(value["evidence_pack"])
    return Sv9FlowCandidate(
        evidence_pack=BrandEvidencePack(
            **{key: item for key, item in pack.items() if key != "evidence"},
            evidence=[EvidenceRecord(**row) for row in pack.get("evidence") or []],
        ),
        interpretation=BrandInterpretation(**value["interpretation"]),
        tile_signals=[TileSignal(**row) for row in value.get("tile_signals") or []],
        limitations=list(value.get("limitations") or []),
        schema_version=str(value.get("schema_version") or SV9_FLOW_CANDIDATE_VERSION),
        claim_memory_evidence=[EvidenceRecord(**row) for row in value.get("claim_memory_evidence") or []],
        evaluation_evidence_refs={
            key: list(refs) for key, refs in dict(value.get("evaluation_evidence_refs") or {}).items()
        },
        evaluation_evidence_version=str(value.get("evaluation_evidence_version") or ""),
    )


def _support_state(item: Mapping[str, Any], by_pair: Mapping[tuple[str, str], Mapping[str, Any]]) -> dict[str, Any]:
    ref, fingerprint = str(item["evidence_ref"]), str(item["evidence_fingerprint"])
    row = by_pair.get((ref, fingerprint))
    state, reasons = (
        (NOT_VERIFIED, ["supporting_evidence_not_in_ledger"]) if row is None else (row["state"], list(row["reason_codes"]))
    )
    return {"evidence_ref": ref, "evidence_fingerprint": fingerprint, "state": state, "reason_codes": reasons}


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _normalized(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _line_key(line: str) -> str:
    return " ".join(line.split()).lower()


def _shingles(text: str) -> set[str]:
    return set(_shingle_list(text))


def _shingle_list(text: str) -> list[str]:
    words = _WORD.findall(unicodedata.normalize("NFKC", str(text or "")).casefold())
    return [" ".join(words[index : index + _SHINGLE_WORDS]) for index in range(len(words) - _SHINGLE_WORDS + 1)]
