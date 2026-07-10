"""Identity resolution helpers for Brand Intelligence."""

from __future__ import annotations

from typing import Any

from src.research.brand_intelligence import (
    BrandEntityType,
    BrandIdentityBakeoffCase,
    BrandIdentityBakeoffResult,
    BrandIdentityResolution,
    BrandIdentitySignal,
    BrandSeed,
    ResolvedBrandEntity,
)
from src.research.brand_intelligence_identity_support import (
    _analysis_mode_for_candidate,
    _candidate_from_search_title,
    _candidate_from_signal_group,
    _candidates_from_signals,
    _clean_candidate_name,
    _coerce_float,
    _edit_distance_at_most,
    _entity_from_identity_resolution,
    _entity_match_anchors,
    _evidence,
    _evaluate_identity_case,
    _external_result_matches_entity,
    _external_result_text,
    _host,
    _identity_resolution,
    _is_ambiguous_name,
    _label_from_host,
    _known_parent_url,
    _looks_like_placeholder,
    _match_text,
    _match_tokens,
    _near_entity_token_collision,
    _normalize_name,
    _normalize_url,
    _parent_from_text,
    _resolution_status_for_candidate,
    _root_domain,
    _source_url_key,
    _strongest_entity_type,
    _title_key,
    _unresolved,
    _unique,
)


def resolve_brand_seed(seed: BrandSeed) -> ResolvedBrandEntity:
    """Resolve a brand seed without pretending all seeds identify a brand."""
    identity = resolve_brand_identity(seed)
    return _entity_from_identity_resolution(identity)


def resolve_brand_identity(seed: BrandSeed) -> BrandIdentityResolution:
    """Return identity candidates for a seed before committing to an entity."""
    return resolve_brand_identity_from_signals(seed, identity_signals_for_seed(seed))


def resolve_brand_identity_from_signals(
    seed: BrandSeed,
    signals: list[BrandIdentitySignal],
) -> BrandIdentityResolution:
    """Resolve identity candidates from observed or fixture identity signals."""
    if seed.kind == "manual_text":
        return BrandIdentityResolution(
            input_seed=seed,
            status="unresolved",
            missing=["verified_identity_seed", "canonical_owned_surface"],
            limitations=["manual_text_without_identity", "requires_multichannel_validation"],
        )

    candidates = _candidates_from_signals(signals)
    if not candidates:
        return BrandIdentityResolution(
            input_seed=seed,
            status="unresolved",
            missing=["identity_candidates", "canonical_owned_surface"],
            limitations=["insufficient_identity_signal", "requires_multichannel_validation"],
        )
    selected = candidates[0]
    status = _resolution_status_for_candidate(selected)
    missing: list[str] = []
    limitations = list(selected.limitations)
    if status == "provisional":
        missing.append("verified_entity_resolution")
        limitations.append("requires_multichannel_validation")
        if "subdomain_surface_needs_parent_validation" in selected.limitations:
            missing.append("verified_parent_surface")
        if not selected.canonical_url:
            missing.append("canonical_owned_surface")
    return _identity_resolution(seed, status, candidates, selected=selected, missing=missing, limitations=limitations)


def identity_signals_for_seed(seed: BrandSeed) -> list[BrandIdentitySignal]:
    """Build deterministic offline identity signals for contract tests.

    Later provider adapters can produce this same shape from owned web, search,
    LinkedIn, reviews, app stores, or other channels.
    """
    value = (seed.value or "").strip()
    name = (seed.provided_name or "").strip()
    canonical_url = _normalize_url(value) if seed.kind in {"url", "linkedin", "app_store"} else ""
    host = _host(canonical_url or value)
    root = _root_domain(host)
    match_key = _normalize_name(name or _label_from_host(host) or value)
    if seed.kind == "manual_text":
        return []

    signals: list[BrandIdentitySignal] = []

    if match_key == "chatgpt" or root == "chatgpt.com":
        return [
            BrandIdentitySignal("domain", "ChatGPT", 0.9, root or host, "product", canonical_url or "https://chatgpt.com", "OpenAI", "https://openai.com", "ChatGPT", canonical_url or "https://chatgpt.com", "known_product_domain"),
            BrandIdentitySignal("owned_web", "ChatGPT", 0.95, "ChatGPT by OpenAI", "product", canonical_url or "https://chatgpt.com", "OpenAI", "https://openai.com", "ChatGPT", canonical_url or "https://chatgpt.com", "owned_parent_reference"),
            BrandIdentitySignal("search", "ChatGPT", 0.9, "OpenAI ChatGPT", "product", canonical_url or "https://chatgpt.com", "OpenAI", "https://openai.com", "ChatGPT", canonical_url or "https://chatgpt.com", "search_parent_reference"),
        ]

    if match_key == "langchain" or root == "langchain.com":
        return [
            BrandIdentitySignal("domain", "LangChain", 0.85, root or host, "company", canonical_url or "https://www.langchain.com", signal="known_company_domain"),
            BrandIdentitySignal("owned_web", "LangChain", 0.9, "LangChain product platform", "company", canonical_url or "https://www.langchain.com", signal="owned_company_surface"),
            BrandIdentitySignal("search", "LangChain", 0.86, "LangChain AI app development platform", "company", canonical_url or "https://www.langchain.com", signal="search_company_reference"),
        ]

    if match_key == "base" or root == "base.org":
        return [
            BrandIdentitySignal("domain", "Base", 0.85, root or host, "ecosystem", canonical_url or "https://base.org", signal="known_ecosystem_domain"),
            BrandIdentitySignal("search", "Base", 0.9, "Base protocol ecosystem", "ecosystem", canonical_url or "https://base.org", signal="search_ecosystem_reference"),
        ]

    if root == "naturaumana.ai" or root == "lab.naturaumana.ai":
        return [
            BrandIdentitySignal("domain", "Natura Umana", 0.58, root or host, "company", canonical_url or "https://www.naturaumana.ai", signal="domain_family_match"),
            BrandIdentitySignal("owned_web", "Natura Umana", 0.62, "lab surface in naturaumana.ai domain family", "company", canonical_url or "https://www.naturaumana.ai", signal="subdomain_surface"),
        ]

    if seed.kind == "name" and _is_ambiguous_name(value):
        return [
            BrandIdentitySignal("manual", value, 0.25, value, "unknown", signal="ambiguous_name"),
        ]

    if canonical_url and not _looks_like_placeholder(root):
        label = name or _label_from_host(host)
        signals.append(
            BrandIdentitySignal("domain", label or root or "Unknown", 0.35, root or host, "unknown", canonical_url, signal="url_seed")
        )
    return signals


def domain_identity_signal(url: str, *, provided_name: str = "") -> BrandIdentitySignal | None:
    canonical_url = _normalize_url(url)
    host = _host(canonical_url)
    root = _root_domain(host)
    if not canonical_url or _looks_like_placeholder(root):
        return None
    name = (provided_name or _label_from_host(host) or root).strip()
    confidence = 0.55 if provided_name and _normalize_name(provided_name) in _normalize_name(root) else 0.35
    return BrandIdentitySignal(
        source="domain",
        candidate_name=name,
        confidence=confidence,
        value=root or host,
        entity_type="unknown",
        canonical_url=canonical_url,
        signal="domain_seed",
    )


def owned_web_identity_signal(payload: dict[str, object]) -> BrandIdentitySignal | None:
    name = str(payload.get("brand_name") or payload.get("title") or "").strip()
    url = str(payload.get("canonical_url") or payload.get("url") or "").strip()
    title = str(payload.get("title") or "").strip()
    description = str(payload.get("description") or payload.get("meta_description") or "").strip()
    if not name and not title:
        return None
    candidate = _clean_candidate_name(name or title)
    parent = _parent_from_text(" ".join([title, description]))
    entity_type: BrandEntityType = "product" if parent else "company"
    return BrandIdentitySignal(
        source="owned_web",
        candidate_name=candidate,
        confidence=0.86 if url else 0.72,
        value=title or description or candidate,
        entity_type=entity_type,
        canonical_url=_normalize_url(url) if url else "",
        parent_brand=parent,
        parent_url=_known_parent_url(parent),
        product_name=candidate if parent else None,
        product_url=_normalize_url(url) if parent and url else None,
        signal="owned_title_or_meta",
    )


def search_result_identity_signal(result: dict[str, object]) -> BrandIdentitySignal | None:
    title = str(result.get("title") or "").strip()
    url = str(result.get("url") or "").strip()
    text = str(result.get("text") or result.get("summary") or "").strip()
    candidate = _candidate_from_search_title(title)
    if not candidate:
        return None
    parent = _parent_from_text(" ".join([title, text]))
    return BrandIdentitySignal(
        source="search",
        candidate_name=candidate,
        confidence=0.82 if parent or url else 0.68,
        value=title or text,
        entity_type="product" if parent else "unknown",
        canonical_url=_normalize_url(url) if url else "",
        parent_brand=parent,
        parent_url=_known_parent_url(parent),
        product_name=candidate if parent else None,
        product_url=_normalize_url(url) if parent and url else None,
        signal="search_result",
    )


def linkedin_identity_signal(profile: dict[str, object]) -> BrandIdentitySignal | None:
    name = str(profile.get("name") or profile.get("company_name") or "").strip()
    description = str(profile.get("description") or "").strip()
    if not name:
        return None
    return BrandIdentitySignal(
        source="linkedin",
        candidate_name=_clean_candidate_name(name),
        confidence=0.84,
        value=description or name,
        entity_type="company",
        canonical_url=_normalize_url(str(profile.get("website") or "")),
        signal="company_profile",
    )


def review_identity_signal(listing: dict[str, object]) -> BrandIdentitySignal | None:
    name = str(listing.get("name") or listing.get("product_name") or "").strip()
    url = str(listing.get("url") or "").strip()
    category = str(listing.get("category") or "").strip()
    if not name:
        return None
    return BrandIdentitySignal(
        source="reviews",
        candidate_name=_clean_candidate_name(name),
        confidence=0.7,
        value=category or name,
        entity_type="product",
        canonical_url=_normalize_url(url) if url else "",
        product_name=_clean_candidate_name(name),
        signal="review_listing",
    )
def evaluate_brand_identity_bakeoff(cases: list[BrandIdentityBakeoffCase]) -> dict[str, object]:
    results = [_evaluate_identity_case(case) for case in cases]
    total = len(results)
    passed = sum(1 for result in results if result.passed)
    known = [result for result in results if result.known_case]
    unknown = [result for result in results if not result.known_case]
    return {
        "version": "brand_identity_bakeoff_v0_1",
        "case_count": total,
        "passed_count": passed,
        "accuracy": round(passed / total, 4) if total else 0.0,
        "known_case_count": len(known),
        "unknown_case_count": len(unknown),
        "known_accuracy": round(sum(1 for result in known if result.passed) / len(known), 4) if known else 0.0,
        "unknown_accuracy": round(sum(1 for result in unknown if result.passed) / len(unknown), 4) if unknown else 0.0,
        "misresolved_count": sum(1 for result in results if result.misresolved),
        "results": [result.to_dict() for result in results],
    }
