"""Shared DOM obstruction patterns and context helpers."""

from __future__ import annotations

import re


COOKIE_TERMS = (
    "cookie",
    "cookies",
    "consent",
    "privacy",
    "gdpr",
    "ccpa",
    "onetrust",
    "trustarc",
    "usercentrics",
    "cookiebot",
    "didomi",
    "quantcast",
    "cmp",
)
NEWSLETTER_TERMS = ("newsletter", "subscribe", "subscription", "email signup")
LOGIN_TERMS = ("login", "log in", "sign in", "signin", "create account", "members only", "paywall")
PROMO_TERMS = ("promo", "promotion", "discount", "offer", "sale", "coupon")
OVERLAY_TERMS = (
    "modal",
    "dialog",
    "overlay",
    "backdrop",
    "popup",
    "pop-up",
    "popover",
    "aria-modal",
    "role=\"dialog",
    "role='dialog",
)

FIXED_LIKE_RE = re.compile(r"position\s*:\s*fixed|\bfixed\b|inset-0|fixed-bottom|bottom-0|sticky")
BOTTOM_LIKE_RE = re.compile(r"bottom\s*:\s*0|bottom-0|fixed-bottom|cookie[-_\s]?bar|consent[-_\s]?bar")
FULL_LIKE_RE = re.compile(r"inset\s*:\s*0|inset-0|height\s*:\s*100(?:vh|%)|min-height\s*:\s*100vh|w-screen|h-screen")
HIGH_Z_RE = re.compile(r"z-index\s*:\s*(?:[9]\d{2,}|\d{4,})|z-\[?\d{3,}\]?|z-50")
OVERLAY_CUES_RE = re.compile(
    r"modal|dialog|overlay|backdrop|popup|pop-up|popover|aria-modal|role=['\"]?dialog|"
    r"position\s*:\s*fixed|fixed-bottom|bottom-0|inset-0|z-\[?\d{3,}\]?|z-50|sticky"
)
PAGE_CUES_RE = re.compile(
    r"<(header|nav|main|section|article|footer)\b|site-header|site-nav|navbar|topbar|masthead|breadcrumb|menu|"
    r"utility-nav|primary-nav|secondary-nav|header__|nav__"
)
HEIGHT_VH_RE = re.compile(r"height\s*:\s*(\d+(?:\.\d+)?)(vh|%)")
HEIGHT_PX_RE = re.compile(r"height\s*:\s*(\d+(?:\.\d+)?)px")


def split_term_signals(text: str, terms: tuple[str, ...], *, signal_prefix: str) -> tuple[list[str], list[str]]:
    page_level_signals: list[str] = []
    overlay_level_signals: list[str] = []
    for term in terms:
        for context in term_contexts(text, term):
            signal = f"{signal_prefix}:{term}"
            cue_kind = _nearest_cue_kind(context, term)
            if cue_kind == "overlay":
                overlay_level_signals.append(signal)
            else:
                page_level_signals.append(signal)
    return page_level_signals, overlay_level_signals


def _nearest_cue_kind(context: str, term: str) -> str:
    term_index = context.find(term)
    if term_index < 0:
        term_index = len(context) // 2
    overlay_distance = _nearest_match_distance(OVERLAY_CUES_RE, context, term_index)
    page_distance = _nearest_match_distance(PAGE_CUES_RE, context, term_index)
    if overlay_distance is not None and (page_distance is None or overlay_distance <= page_distance):
        return "overlay"
    if page_distance is not None:
        return "page"
    return "page"


def _nearest_match_distance(pattern: re.Pattern[str], context: str, term_index: int) -> int | None:
    distances = [abs(match.start() - term_index) for match in pattern.finditer(context)]
    return min(distances) if distances else None


def term_contexts(text: str, term: str, *, window: int = 220, limit: int = 3) -> list[str]:
    contexts: list[str] = []
    for match in re.finditer(re.escape(term), text):
        start = max(0, match.start() - window)
        end = min(len(text), match.end() + window)
        contexts.append(text[start:end])
        if len(contexts) >= limit:
            break
    return contexts


def context_has_overlay_cues(context: str) -> bool:
    return bool(OVERLAY_CUES_RE.search(context))


def context_has_page_level_cues(context: str) -> bool:
    return bool(PAGE_CUES_RE.search(context))
