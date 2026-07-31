"""Viewport obstruction heuristics for Visual Signature evidence quality.

This module detects likely cookie banners, modals, login walls, and overlays
that can compromise first-impression visual analysis. It is evidence-only: it
does not click, dismiss, mutate DOM, bypass protections, or affect scoring.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from src.visual_signature.vision.types import RasterImage
from src.visual_signature._internal.utils import float_or_none as _float_or_none, unique as _unique
from src.visual_signature._internal.viewport_obstruction_patterns import (
    BOTTOM_LIKE_RE,
    COOKIE_TERMS,
    FIXED_LIKE_RE,
    FULL_LIKE_RE,
    HEIGHT_PX_RE,
    HEIGHT_VH_RE,
    HIGH_Z_RE,
    LOGIN_TERMS,
    NEWSLETTER_TERMS,
    OVERLAY_TERMS,
    PROMO_TERMS,
    context_has_overlay_cues as _context_has_overlay_cues,
    split_term_signals as _split_term_signals,
)
from src.visual_signature._internal.viewport_obstruction_pixels import (
    bottom_bar_ratio as _bottom_bar_ratio,
    centered_modal_score as _centered_modal_score,
    fullscreen_overlay_score as _fullscreen_overlay_score,
)


ObstructionType = Literal[
    "cookie_banner",
    "cookie_modal",
    "newsletter_modal",
    "login_wall",
    "promo_modal",
    "unknown_overlay",
    "none",
]
ObstructionSeverity = Literal["minor", "moderate", "major", "blocking", "none"]

@dataclass
class ViewportObstructionEvidence:
    present: bool
    type: ObstructionType = "none"
    severity: ObstructionSeverity = "none"
    coverage_ratio: float = 0.0
    first_impression_valid: bool = True
    confidence: float = 0.0
    page_level_signals: list[str] = field(default_factory=list)
    overlay_level_signals: list[str] = field(default_factory=list)
    visual_signals: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if payload["type"] == "none":
            payload["type"] = "unknown_overlay" if payload["present"] else "none"
        return payload


def analyze_viewport_obstruction(
    *,
    dom_html: str | None = None,
    viewport_image: RasterImage | None = None,
    existing_obstruction: dict[str, Any] | None = None,
) -> ViewportObstructionEvidence:
    """Combine DOM and viewport heuristics into an obstruction evidence record."""
    dom = _dom_obstruction(dom_html or "")
    viewport = _viewport_obstruction(viewport_image)
    existing = _coerce_existing(existing_obstruction)

    page_level_signals = _unique(existing.page_level_signals + dom.page_level_signals + viewport.page_level_signals)
    overlay_level_signals = _unique(existing.overlay_level_signals + dom.overlay_level_signals + viewport.overlay_level_signals)
    visual_signals = _unique(existing.visual_signals + dom.visual_signals + viewport.visual_signals)
    signals = _unique(existing.signals + dom.signals + viewport.signals + page_level_signals + overlay_level_signals + visual_signals)
    limitations = _unique(existing.limitations + dom.limitations + viewport.limitations)
    present = existing.present or dom.present or viewport.present
    obstruction_type = _choose_type(existing.type, dom.type, viewport.type)
    coverage_ratio = max(existing.coverage_ratio, dom.coverage_ratio, viewport.coverage_ratio)
    viewport_only_unconfirmed = (
        viewport.present
        and not dom.present
        and not existing.present
    )
    if viewport_only_unconfirmed:
        obstruction_type = "unknown_overlay"
        coverage_ratio = min(coverage_ratio, 0.12)
    dom_only_unconfirmed_by_viewport = _dom_only_unconfirmed_by_viewport(
        existing=existing,
        dom=dom,
        viewport=viewport,
        viewport_image=viewport_image,
        obstruction_type=obstruction_type,
        coverage_ratio=coverage_ratio,
    )
    if dom_only_unconfirmed_by_viewport:
        obstruction_type = "unknown_overlay"
        coverage_ratio = min(coverage_ratio, 0.12)

    if not present and signals:
        limitations.append("weak_obstruction_signals_below_presence_threshold")
    if dom_only_unconfirmed_by_viewport:
        limitations.append("dom_only_obstruction_unconfirmed_by_viewport")
    if viewport_only_unconfirmed:
        limitations.append("viewport_only_obstruction_unconfirmed_by_dom")
    severity = _severity(coverage_ratio, obstruction_type, present)
    first_impression_valid = not (
        severity in {"major", "blocking"}
        or obstruction_type == "login_wall"
        or coverage_ratio >= 0.45
        or (existing.first_impression_valid is False and not dom_only_unconfirmed_by_viewport)
    )
    confidence = _confidence(
        present=present,
        dom_present=dom.present or existing.present,
        viewport_present=viewport.present,
        obstruction_type=obstruction_type,
        coverage_ratio=coverage_ratio,
        signal_count=len(signals),
    )
    if dom_only_unconfirmed_by_viewport:
        confidence = min(confidence, 0.45)
    if viewport_only_unconfirmed:
        confidence = min(confidence, 0.45)
    if viewport_image is None:
        limitations.append("viewport_pixels_unavailable_for_obstruction_analysis")
    if not dom_html and not existing.signals:
        limitations.append("dom_obstruction_signals_unavailable")

    return ViewportObstructionEvidence(
        present=present,
        type=obstruction_type if present else "none",
        severity=severity,
        coverage_ratio=round(min(1.0, max(0.0, coverage_ratio)), 3),
        first_impression_valid=first_impression_valid,
        confidence=confidence,
        page_level_signals=page_level_signals,
        overlay_level_signals=overlay_level_signals,
        visual_signals=visual_signals,
        signals=signals,
        limitations=_unique(limitations),
    )


def _dom_only_unconfirmed_by_viewport(
    *,
    existing: ViewportObstructionEvidence,
    dom: ViewportObstructionEvidence,
    viewport: ViewportObstructionEvidence,
    viewport_image: RasterImage | None,
    obstruction_type: str,
    coverage_ratio: float,
) -> bool:
    if viewport_image is None:
        return False
    if viewport.present:
        return False
    if viewport.visual_signals:
        return False
    if not existing.present:
        return False
    if not any(
        item in {"viewport_pixels_unavailable", "viewport_pixels_unavailable_for_obstruction_analysis"}
        for item in existing.limitations
    ):
        return False
    if obstruction_type == "login_wall":
        return False
    if coverage_ratio < 0.45:
        return False
    return True


def _dom_obstruction(html: str) -> ViewportObstructionEvidence:
    text = (html or "").lower()
    if not text.strip():
        return ViewportObstructionEvidence(
            present=False,
            confidence=0.0,
            limitations=["dom_html_unavailable"],
        )

    page_level_signals: list[str] = []
    overlay_level_signals: list[str] = []
    visual_signals: list[str] = []
    cookie_page_hits, cookie_overlay_hits = _split_term_signals(text, COOKIE_TERMS, signal_prefix="dom_keyword")
    newsletter_page_hits, newsletter_overlay_hits = _split_term_signals(text, NEWSLETTER_TERMS, signal_prefix="dom_keyword")
    login_page_hits, login_overlay_hits = _split_term_signals(text, LOGIN_TERMS, signal_prefix="dom_keyword")
    promo_page_hits, promo_overlay_hits = _split_term_signals(text, PROMO_TERMS, signal_prefix="dom_keyword")
    overlay_page_hits, overlay_overlay_hits = _split_term_signals(text, OVERLAY_TERMS, signal_prefix="dom_overlay_term")
    fixed_like = bool(FIXED_LIKE_RE.search(text))
    bottom_like = bool(BOTTOM_LIKE_RE.search(text))
    full_like = bool(FULL_LIKE_RE.search(text))
    high_z = bool(HIGH_Z_RE.search(text))

    page_level_signals.extend(cookie_page_hits[:4])
    page_level_signals.extend(newsletter_page_hits[:3])
    page_level_signals.extend(login_page_hits[:3])
    page_level_signals.extend(promo_page_hits[:3])
    page_level_signals.extend(overlay_page_hits[:4])
    overlay_level_signals.extend(cookie_overlay_hits[:4])
    overlay_level_signals.extend(newsletter_overlay_hits[:3])
    overlay_level_signals.extend(login_overlay_hits[:3])
    overlay_level_signals.extend(promo_overlay_hits[:3])
    overlay_level_signals.extend(overlay_overlay_hits[:4])
    if fixed_like:
        visual_signals.append("dom_fixed_or_sticky_position_pattern")
    if bottom_like:
        visual_signals.append("dom_bottom_aligned_container_pattern")
    if full_like:
        visual_signals.append("dom_full_viewport_container_pattern")
    if high_z:
        visual_signals.append("dom_high_z_index_pattern")

    obstruction_type: ObstructionType = "none"
    overlay_local_login = bool(login_overlay_hits)
    strong_overlay = _context_has_overlay_cues(text) or fixed_like or bottom_like or full_like or high_z
    overlay_local_newsletter = bool(newsletter_overlay_hits)
    overlay_local_promo = bool(promo_overlay_hits)
    overlay_local_cookie = bool(cookie_overlay_hits)
    visible_candidate_snapshot = "<visible-candidate" in text
    if overlay_local_login and strong_overlay:
        obstruction_type = "login_wall"
    elif overlay_local_newsletter and strong_overlay:
        obstruction_type = "newsletter_modal"
    elif overlay_local_promo and strong_overlay:
        obstruction_type = "promo_modal"
    elif overlay_local_cookie and strong_overlay:
        obstruction_type = "cookie_modal" if not bottom_like else "cookie_banner"
    elif (
        visible_candidate_snapshot
        and overlay_level_signals
        and (fixed_like or high_z or full_like or bottom_like)
    ):
        obstruction_type = "unknown_overlay"
    elif visible_candidate_snapshot and fixed_like and bottom_like and high_z:
        obstruction_type = "unknown_overlay"

    present = obstruction_type != "none"
    coverage = 0.0
    if present:
        coverage = _dom_coverage(
            text,
            obstruction_type,
            bottom_like=bottom_like,
            full_like=full_like,
            overlay=bool(overlay_level_signals),
        )

    limitations: list[str] = []
    if (cookie_page_hits or login_page_hits or newsletter_page_hits or promo_page_hits) and not present:
        limitations.append("semantic_terms_without_local_overlay_pattern")
    return ViewportObstructionEvidence(
        present=present,
        type=obstruction_type if present else "none",
        coverage_ratio=coverage,
        confidence=0.0,
        page_level_signals=_unique(page_level_signals),
        overlay_level_signals=_unique(overlay_level_signals),
        visual_signals=_unique(visual_signals),
        signals=_unique(page_level_signals + overlay_level_signals + visual_signals),
        limitations=limitations,
    )


def _viewport_obstruction(image: RasterImage | None) -> ViewportObstructionEvidence:
    if image is None or image.width <= 0 or image.height <= 0 or not image.sample_pixels(1):
        return ViewportObstructionEvidence(
            present=False,
            confidence=0.0,
            limitations=["viewport_pixels_unavailable"],
        )

    visual_signals: list[str] = []
    coverage = 0.0
    obstruction_type: ObstructionType = "none"

    centered_modal = _centered_modal_score(image)
    if centered_modal >= 0.65:
        visual_signals.append("viewport_centered_modal_with_backdrop")
        coverage = max(coverage, min(0.72, centered_modal))
        obstruction_type = "unknown_overlay"

    full_overlay_score = _fullscreen_overlay_score(image)
    if full_overlay_score >= 0.86:
        visual_signals.append("viewport_fullscreen_overlay_pattern")
        # A dark or single-color viewport can be a legitimate visual system.
        # Treat fullscreen darkness as supporting evidence; DOM/existing
        # obstruction signals decide whether it is actually an overlay.

    bottom_ratio = _bottom_bar_ratio(image)
    if bottom_ratio >= 0.07:
        visual_signals.append("viewport_bottom_bar_pattern")
        coverage = max(coverage, bottom_ratio)
        if obstruction_type == "none":
            obstruction_type = "unknown_overlay"

    present = coverage >= 0.12 or centered_modal >= 0.65 or full_overlay_score >= 0.86
    if bottom_ratio and bottom_ratio < 0.12:
        visual_signals.append("viewport_minor_sticky_footer_pattern")
        present = present or bottom_ratio >= 0.05
        coverage = max(coverage, bottom_ratio)
        if obstruction_type == "none":
            obstruction_type = "unknown_overlay"

    return ViewportObstructionEvidence(
        present=present,
        type=obstruction_type if present else "none",
        coverage_ratio=coverage if present else 0.0,
        confidence=0.0,
        visual_signals=_unique(visual_signals),
        signals=_unique(visual_signals),
        limitations=[],
    )


def _coerce_existing(value: dict[str, Any] | None) -> ViewportObstructionEvidence:
    if not isinstance(value, dict):
        return ViewportObstructionEvidence(present=False)
    return ViewportObstructionEvidence(
        present=bool(value.get("present")),
        type=_valid_type(value.get("type")),
        severity=_valid_severity(value.get("severity")),
        coverage_ratio=_float_or_none(value.get("coverage_ratio")) or 0.0,
        first_impression_valid=bool(value.get("first_impression_valid", True)),
        confidence=_float_or_none(value.get("confidence")) or 0.0,
        page_level_signals=[str(item) for item in value.get("page_level_signals") or []],
        overlay_level_signals=[str(item) for item in value.get("overlay_level_signals") or []],
        visual_signals=[str(item) for item in value.get("visual_signals") or []],
        signals=[str(item) for item in value.get("signals") or []],
        limitations=[str(item) for item in value.get("limitations") or []],
    )


def _dom_coverage(text: str, obstruction_type: str, *, bottom_like: bool, full_like: bool, overlay: bool) -> float:
    if obstruction_type == "login_wall" or full_like:
        return 0.92
    if overlay:
        return 0.55
    if bottom_like:
        height_match = HEIGHT_VH_RE.search(text)
        if height_match:
            value = float(height_match.group(1))
            return min(0.45, max(0.06, value / 100))
        px_match = HEIGHT_PX_RE.search(text)
        if px_match:
            return min(0.35, max(0.04, float(px_match.group(1)) / 900))
        return 0.18 if obstruction_type == "cookie_banner" else 0.07
    return 0.22


def _choose_type(*values: str) -> ObstructionType:
    priority = {
        "login_wall": 6,
        "cookie_modal": 5,
        "newsletter_modal": 4,
        "promo_modal": 3,
        "cookie_banner": 2,
        "unknown_overlay": 1,
        "none": 0,
    }
    chosen = max((_valid_type(value) for value in values), key=lambda item: priority[item])
    return chosen


def _severity(coverage_ratio: float, obstruction_type: str, present: bool) -> ObstructionSeverity:
    if not present:
        return "none"
    if obstruction_type == "login_wall" or coverage_ratio >= 0.85:
        return "blocking"
    if coverage_ratio >= 0.45:
        return "major"
    if coverage_ratio >= 0.16:
        return "moderate"
    return "minor"


def _confidence(
    *,
    present: bool,
    dom_present: bool,
    viewport_present: bool,
    obstruction_type: str,
    coverage_ratio: float,
    signal_count: int,
) -> float:
    if not present:
        return 0.25 if signal_count else 0.0
    score = 0.35
    if dom_present:
        score += 0.22
    if viewport_present:
        score += 0.22
    if obstruction_type != "unknown_overlay":
        score += 0.12
    if coverage_ratio >= 0.45:
        score += 0.08
    score += min(0.12, signal_count * 0.025)
    return round(max(0.0, min(1.0, score)), 3)


def _valid_type(value: Any) -> ObstructionType:
    text = str(value or "none")
    allowed = {
        "cookie_banner",
        "cookie_modal",
        "newsletter_modal",
        "login_wall",
        "promo_modal",
        "unknown_overlay",
        "none",
    }
    return text if text in allowed else "unknown_overlay"  # type: ignore[return-value]


def _valid_severity(value: Any) -> ObstructionSeverity:
    text = str(value or "none")
    allowed = {"minor", "moderate", "major", "blocking", "none"}
    return text if text in allowed else "none"  # type: ignore[return-value]
