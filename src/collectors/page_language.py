"""Deterministic English/Spanish page-language evidence.

The detector is deliberately bounded. It only classifies English and Spanish
when the captured page contains enough function-word evidence; every other
case remains ``und`` instead of guessing a language from a title or URL.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

PAGE_LANGUAGE_DETECTION_VERSION = "owned-page-language-en-es-v1"

_MIN_TOKEN_COUNT = 24
_MIN_LANGUAGE_HITS = 4
_MIXED_RATIO = 1.35
_HIGH_CONFIDENCE_HITS = 8
_HIGH_CONFIDENCE_RATIO = 1.8

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_LANG_RE = re.compile(
    r"<html\b[^>]*\blang\s*=\s*[\"']?\s*([a-zA-Z]{2,8}(?:-[a-zA-Z0-9]{2,8})?)",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[a-záéíóúüñ]+", re.IGNORECASE)

_LANGUAGE_MARKERS = {
    "en": frozenset(
        {
            "about",
            "all",
            "also",
            "and",
            "are",
            "as",
            "at",
            "be",
            "because",
            "been",
            "before",
            "between",
            "by",
            "can",
            "club",
            "clubs",
            "do",
            "does",
            "for",
            "from",
            "has",
            "have",
            "help",
            "how",
            "in",
            "into",
            "is",
            "it",
            "more",
            "not",
            "of",
            "on",
            "only",
            "our",
            "than",
            "that",
            "the",
            "their",
            "this",
            "through",
            "to",
            "we",
            "what",
            "when",
            "with",
            "you",
            "your",
        }
    ),
    "es": frozenset(
        {
            "además",
            "al",
            "antes",
            "año",
            "años",
            "club",
            "clubes",
            "como",
            "con",
            "cuando",
            "de",
            "del",
            "desde",
            "el",
            "en",
            "entre",
            "es",
            "esta",
            "este",
            "fútbol",
            "ha",
            "han",
            "hasta",
            "la",
            "las",
            "lo",
            "los",
            "más",
            "no",
            "nuestro",
            "nuestra",
            "para",
            "pero",
            "por",
            "porque",
            "que",
            "se",
            "sin",
            "son",
            "su",
            "sus",
            "también",
            "todo",
            "una",
            "uno",
            "y",
            "ya",
        }
    ),
}


def detect_page_language(markdown: Any, *, html: Any = "") -> dict[str, Any]:
    """Return observed and declared language evidence for one captured page."""

    text = _HTML_TAG_RE.sub(" ", _URL_RE.sub(" ", str(markdown or "")))
    tokens = [token.casefold() for token in _TOKEN_RE.findall(text)]
    token_counts = Counter(tokens)
    scores = {
        language: sum(token_counts[token] for token in markers)
        for language, markers in _LANGUAGE_MARKERS.items()
    }
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_language, best_score = ranked[0]
    second_score = ranked[1][1]

    observed = "und"
    confidence = "low"
    if len(tokens) >= _MIN_TOKEN_COUNT and best_score >= _MIN_LANGUAGE_HITS:
        if (
            second_score >= _MIN_LANGUAGE_HITS
            and best_score <= second_score * _MIXED_RATIO
        ):
            observed = "mixed_en_es"
            confidence = "medium"
        else:
            observed = best_language
            confidence = (
                "high"
                if best_score >= _HIGH_CONFIDENCE_HITS
                and (
                    second_score == 0
                    or best_score >= second_score * _HIGH_CONFIDENCE_RATIO
                )
                else "medium"
            )

    declared = _declared_language(html)
    return {
        "version": PAGE_LANGUAGE_DETECTION_VERSION,
        "observed_language": observed,
        "confidence": confidence,
        "declared_language": declared,
        "token_count": len(tokens),
        "scores": scores,
        "declared_mismatch": bool(
            declared in {"en", "es"}
            and observed in {"en", "es"}
            and declared != observed
        ),
    }


def summarize_page_languages(rows: Any) -> dict[str, Any]:
    """Aggregate captured page-language evidence without inferring missing rows."""

    captured = [
        row
        for row in rows or []
        if isinstance(row, dict) and str(row.get("status") or "") == "captured"
    ]
    counts = Counter(
        str(row.get("observed_language") or "und")
        for row in captured
    )
    evaluated_count = sum(
        count
        for language, count in counts.items()
        if language != "und"
    )
    distribution = [
        {
            "language": language,
            "page_count": count,
            "share": (
                round(count / evaluated_count, 4)
                if evaluated_count and language != "und"
                else 0.0
            ),
        }
        for language, count in sorted(counts.items())
        if count
    ]
    observed_languages = {
        language
        for language in counts
        if language in {"en", "es"}
    }
    mixed = (
        len(observed_languages) > 1
        or counts.get("mixed_en_es", 0) > 0
    )
    return {
        "version": PAGE_LANGUAGE_DETECTION_VERSION,
        "supported_languages": ["en", "es"],
        "status": (
            "mixed"
            if mixed
            else "single_language"
            if evaluated_count
            else "insufficient_text"
        ),
        "mixed_language_site": mixed,
        "captured_page_count": len(captured),
        "evaluated_page_count": evaluated_count,
        "unknown_page_count": counts.get("und", 0),
        "declared_mismatch_count": sum(
            1 for row in captured if row.get("declared_mismatch") is True
        ),
        "distribution": distribution,
    }


def _declared_language(html: Any) -> str:
    match = _HTML_LANG_RE.search(str(html or ""))
    if not match:
        return ""
    return match.group(1).split("-", 1)[0].lower()
