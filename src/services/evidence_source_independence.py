"""Conservative source-independence projection for external evidence.

The projection intentionally treats missing relationship data as unknown. It
can collapse sources conservatively, but it cannot grant runtime corroboration
or scoring authority.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlparse

from src.evidence_identity import stable_artifact_digest


EVIDENCE_SOURCE_INDEPENDENCE_VERSION = "evidence-source-independence-v3"
_POLICY_PATH = Path(__file__).with_name("evidence_source_registry_v1.json")
_KNOWN_GROUP_KINDS = {"editorial", "wire_service"}
_KNOWN_REVIEW_STATES = {"confirmed", "fixture"}
_DISTRIBUTION_ALIASES = {
    "editorial": "editorial",
    "original_reporting": "editorial",
    "press_release": "press_release",
    "press-release": "press_release",
    "press release": "press_release",
    "wire": "wire_service",
    "wire_service": "wire_service",
    "wire-service": "wire_service",
}


def source_independence_contract() -> dict[str, Any]:
    """Return the versioned public contract without exposing mutable state."""

    policy = _load_policy()
    reviewed_sources = [source for source in policy.get("reviewed_sources") or [] if isinstance(source, dict)]
    return {
        "schema_version": EVIDENCE_SOURCE_INDEPENDENCE_VERSION,
        "policy_version": str(policy["policy_version"]),
        "registry_version": str(policy["registry_version"]),
        "registry_fingerprint": _policy_fingerprint(policy),
        "runtime_effect": False,
        "authority": False,
        "status_values": [
            "confirmed_independent",
            "same_cluster",
            "disputed",
            "unknown",
        ],
        "shingle_size": int(policy["similarity"]["shingle_size"]),
        "minimum_shingle_count": int(policy["similarity"]["minimum_shingle_count"]),
        "same_cluster_similarity_threshold": float(policy["similarity"]["same_cluster_threshold"]),
        "disputed_similarity_threshold": float(policy["similarity"]["disputed_threshold"]),
        "confirmed_required_for_future_corroboration": True,
        "reviewed_source_count": len(reviewed_sources),
        "production_reviewed_source_count": sum(1 for source in reviewed_sources if not source.get("fixture_only")),
        "fixture_reviewed_source_count": sum(1 for source in reviewed_sources if source.get("fixture_only")),
    }


def content_shingle_hashes(content: str) -> tuple[str, ...]:
    """Build a deterministic, non-reversible-enough comparison signature."""

    policy = _load_policy()
    size = int(policy["similarity"]["shingle_size"])
    tokens = re.findall(r"\w+", str(content).casefold(), flags=re.UNICODE)
    if len(tokens) < size:
        return ()
    return tuple(
        sorted(
            {
                hashlib.sha256("\x1f".join(tokens[index : index + size]).encode("utf-8")).hexdigest()
                for index in range(len(tokens) - size + 1)
            }
        )
    )


def normalize_distribution_types(values: Iterable[Any]) -> list[str]:
    """Normalize only explicit, recognized lineage labels."""

    normalized = {
        _DISTRIBUTION_ALIASES[text]
        for value in values
        if (text := str(value or "").strip().lower()) in _DISTRIBUTION_ALIASES
    }
    return sorted(normalized)


def assign_external_source_independence(
    entries: list[dict[str, Any]],
    *,
    current_evidence_ids: set[str],
) -> dict[str, Any]:
    """Annotate current external entries and return deterministic counts."""

    policy = _load_policy()
    domain_registry = _domain_registry(policy)
    reviewed_source_registry = _reviewed_source_registry(policy)
    current = sorted(
        (
            entry
            for entry in entries
            if entry.get("source_class") == "external_proof"
            and str(entry.get("evidence_id") or "") in current_evidence_ids
        ),
        key=lambda entry: str(entry.get("evidence_id") or ""),
    )
    current_ids = [str(entry["evidence_id"]) for entry in current]
    parent = {evidence_id: evidence_id for evidence_id in current_ids}
    entry_by_id = {str(entry["evidence_id"]): entry for entry in current}
    relation_reasons: dict[str, set[str]] = defaultdict(set)
    ambiguous_relations: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def find(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: str, right: str, reason: str) -> None:
        if left != right:
            relation_reasons[left].add(reason)
            relation_reasons[right].add(reason)
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for entry in current:
        source_domain = str(entry.get("source_domain") or "")
        registry = domain_registry.get(source_domain)
        entry["publisher_group_id"] = str(registry["group_id"]) if registry else ""
        entry["publisher_group_kind"] = str(registry["kind"]) if registry else ""
        entry["publisher_registry_status"] = str(registry["review_state"]) if registry else "unreviewed"
        reviewed_source = reviewed_source_registry.get(str(entry.get("url") or ""))
        if reviewed_source and reviewed_source["publisher_group_id"] != entry["publisher_group_id"]:
            reviewed_source = None
        entry["source_independence_review_status"] = (
            str(reviewed_source["review_state"]) if reviewed_source else "unreviewed"
        )

    first_by_relation: dict[tuple[str, str], str] = {}
    for entry in current:
        evidence_id = str(entry["evidence_id"])
        relation_keys: list[tuple[str, str, str]] = []
        publisher_id = str(entry.get("publisher_id") or "")
        if publisher_id:
            relation_keys.append(("publisher", publisher_id, "same_publisher"))
        group_id = str(entry.get("publisher_group_id") or "")
        if group_id:
            relation_keys.append(("publisher_group", group_id, "same_publisher_group"))
        content_hash = str(entry.get("content_hash") or "")
        if content_hash:
            relation_keys.append(("exact_content", content_hash, "exact_content"))
        lineage_urls = {
            str(value)
            for key in ("canonical_urls", "original_source_urls")
            for value in (entry.get(key) or [])
            if str(value)
        }
        relation_keys.extend(("source_lineage", value, "shared_source_lineage") for value in sorted(lineage_urls))
        for kind, value, reason in relation_keys:
            prior = first_by_relation.setdefault((kind, value), evidence_id)
            union(prior, evidence_id, reason)

    same_threshold = float(policy["similarity"]["same_cluster_threshold"])
    disputed_threshold = float(policy["similarity"]["disputed_threshold"])
    minimum_shingles = int(policy["similarity"]["minimum_shingle_count"])
    for index, left in enumerate(current):
        left_id = str(left["evidence_id"])
        left_shingles = set(left.get("_source_independence_shingles") or ())
        if len(left_shingles) < minimum_shingles:
            continue
        for right in current[index + 1 :]:
            right_id = str(right["evidence_id"])
            if str(left.get("content_hash") or "") == str(right.get("content_hash") or ""):
                continue
            right_shingles = set(right.get("_source_independence_shingles") or ())
            if len(right_shingles) < minimum_shingles:
                continue
            similarity = _jaccard(left_shingles, right_shingles)
            if similarity >= same_threshold:
                union(left_id, right_id, "near_duplicate_content")
                _record_similarity(
                    ambiguous_relations,
                    left_id,
                    right_id,
                    similarity,
                    "same_cluster",
                )
            elif similarity >= disputed_threshold:
                relation_reasons[left_id].add("ambiguous_content_similarity")
                relation_reasons[right_id].add("ambiguous_content_similarity")
                _record_similarity(
                    ambiguous_relations,
                    left_id,
                    right_id,
                    similarity,
                    "disputed",
                )

    members_by_root: dict[str, list[str]] = defaultdict(list)
    for evidence_id in current_ids:
        members_by_root[find(evidence_id)].append(evidence_id)

    status_by_cluster: dict[str, str] = {}
    for members in members_by_root.values():
        sorted_members = sorted(members)
        cluster_id = stable_artifact_digest(
            EVIDENCE_SOURCE_INDEPENDENCE_VERSION,
            {"members": sorted_members},
        )
        cluster_reasons = {reason for evidence_id in sorted_members for reason in relation_reasons[evidence_id]}
        is_disputed = any(
            candidate["relation"] == "disputed"
            for evidence_id in sorted_members
            for candidate in ambiguous_relations[evidence_id]
        )
        if is_disputed:
            status = "disputed"
        elif len(sorted_members) > 1:
            status = "same_cluster"
        elif _is_confirmed_independent(entry_by_id[sorted_members[0]]):
            status = "confirmed_independent"
            cluster_reasons.add("reviewed_editorial_publisher_group")
            cluster_reasons.add("reviewed_source_independence_decision")
        else:
            status = "unknown"
            cluster_reasons.update(_unknown_reason_codes(entry_by_id[sorted_members[0]]))
        status_by_cluster[cluster_id] = status
        for evidence_id in sorted_members:
            entry = entry_by_id[evidence_id]
            entry["independence_cluster_id"] = cluster_id
            entry["independence_cluster_member_count"] = len(sorted_members)
            entry["independence_status"] = status
            entry["independence_reason_codes"] = sorted(cluster_reasons)
            entry["independence_requires_human_review"] = status in {
                "disputed",
                "unknown",
            }
            entry["independence_similarity_candidates"] = sorted(
                ambiguous_relations[evidence_id],
                key=lambda item: (
                    str(item["relation"]),
                    str(item["evidence_id"]),
                ),
            )

    for entry in entries:
        entry.pop("_source_independence_shingles", None)
        evidence_id = str(entry.get("evidence_id") or "")
        if evidence_id not in current_evidence_ids:
            entry["independence_cluster_id"] = ""
            entry["independence_cluster_member_count"] = 0
            entry["independence_status"] = "unknown"
            entry["independence_reason_codes"] = ["not_in_current_projection"]
            entry["independence_requires_human_review"] = False
            entry["independence_similarity_candidates"] = []
            entry["publisher_group_id"] = ""
            entry["publisher_group_kind"] = ""
            entry["publisher_registry_status"] = "not_evaluated"
            entry["source_independence_review_status"] = "not_evaluated"

    status_counts = Counter(str(entry.get("independence_status") or "unknown") for entry in current)
    cluster_status_counts = Counter(status_by_cluster.values())
    return {
        "current_external_cluster_count": len(status_by_cluster),
        "current_confirmed_independent_external_cluster_count": (cluster_status_counts.get("confirmed_independent", 0)),
        "current_external_independence_status_counts": dict(sorted(status_counts.items())),
        "current_external_independence_cluster_status_counts": dict(sorted(cluster_status_counts.items())),
    }


@lru_cache(maxsize=1)
def _load_policy() -> dict[str, Any]:
    policy = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    if policy.get("schema_version") != "evidence-source-registry-v1":
        raise ValueError("Unsupported evidence source registry schema")
    similarity = policy.get("similarity")
    if not isinstance(similarity, dict):
        raise ValueError("Evidence source registry similarity policy is required")
    same_threshold = float(similarity.get("same_cluster_threshold") or 0)
    disputed_threshold = float(similarity.get("disputed_threshold") or 0)
    if not 0 < disputed_threshold < same_threshold <= 1:
        raise ValueError("Evidence source similarity thresholds are invalid")
    if int(similarity.get("shingle_size") or 0) < 1:
        raise ValueError("Evidence source shingle size is invalid")
    if int(similarity.get("minimum_shingle_count") or 0) < 1:
        raise ValueError("Evidence source minimum shingle count is invalid")
    _domain_registry(policy)
    _reviewed_source_registry(policy)
    return policy


def _domain_registry(policy: dict[str, Any]) -> dict[str, dict[str, str]]:
    registry: dict[str, dict[str, str]] = {}
    group_ids: set[str] = set()
    for raw_group in policy.get("publisher_groups") or []:
        if not isinstance(raw_group, dict):
            raise ValueError("Publisher group must be an object")
        group_id = str(raw_group.get("id") or "").strip()
        kind = str(raw_group.get("kind") or "").strip()
        review_state = str(raw_group.get("review_state") or "").strip()
        fixture_only = bool(raw_group.get("fixture_only"))
        if not group_id or group_id in group_ids:
            raise ValueError("Publisher group id must be unique")
        if kind not in _KNOWN_GROUP_KINDS:
            raise ValueError(f"Unsupported publisher group kind: {kind}")
        if review_state not in _KNOWN_REVIEW_STATES:
            raise ValueError(f"Unsupported publisher review state: {review_state}")
        if fixture_only != (review_state == "fixture"):
            raise ValueError("Publisher fixture flag and review state must agree")
        group_ids.add(group_id)
        for raw_domain in raw_group.get("domains") or []:
            domain = str(raw_domain or "").strip(".").lower().removeprefix("www.")
            if not domain or domain in registry:
                raise ValueError("Publisher domain must be non-empty and unique")
            if fixture_only and not domain.endswith(".test"):
                raise ValueError("Fixture-only publisher domains must use .test")
            registry[domain] = {
                "group_id": group_id,
                "kind": kind,
                "review_state": review_state,
            }
    return registry


def _reviewed_source_registry(
    policy: dict[str, Any],
) -> dict[str, dict[str, str]]:
    domain_registry = _domain_registry(policy)
    registry: dict[str, dict[str, str]] = {}
    for raw_source in policy.get("reviewed_sources") or []:
        if not isinstance(raw_source, dict):
            raise ValueError("Reviewed source must be an object")
        url = str(raw_source.get("url") or "").strip()
        group_id = str(raw_source.get("publisher_group_id") or "").strip()
        decision = str(raw_source.get("decision") or "").strip()
        review_state = str(raw_source.get("review_state") or "").strip()
        fixture_only = bool(raw_source.get("fixture_only"))
        parsed = urlparse(url)
        hostname = str(parsed.hostname or "").lower()
        if not url or url in registry:
            raise ValueError("Reviewed source URL must be non-empty and unique")
        if parsed.scheme not in {"http", "https"} or not hostname:
            raise ValueError("Reviewed source URL must be absolute HTTP(S)")
        publisher = domain_registry.get(hostname)
        if not publisher or publisher["group_id"] != group_id:
            raise ValueError("Reviewed source URL must belong to its publisher group")
        if decision != "confirmed_independent":
            raise ValueError("Reviewed source decision is unsupported")
        if review_state not in _KNOWN_REVIEW_STATES:
            raise ValueError("Reviewed source review state is unsupported")
        if fixture_only != (review_state == "fixture"):
            raise ValueError("Reviewed source fixture flag and review state must agree")
        if fixture_only and not hostname.endswith(".test"):
            raise ValueError("Fixture-only reviewed sources must use .test")
        registry[url] = {
            "publisher_group_id": group_id,
            "decision": decision,
            "review_state": review_state,
        }
    return registry


def _policy_fingerprint(policy: dict[str, Any]) -> str:
    rendered = json.dumps(
        policy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _record_similarity(
    candidates: dict[str, list[dict[str, Any]]],
    left_id: str,
    right_id: str,
    similarity: float,
    relation: str,
) -> None:
    candidates[left_id].append(
        {
            "evidence_id": right_id,
            "similarity": round(similarity, 4),
            "relation": relation,
        }
    )
    candidates[right_id].append(
        {
            "evidence_id": left_id,
            "similarity": round(similarity, 4),
            "relation": relation,
        }
    )


def _is_confirmed_independent(entry: dict[str, Any]) -> bool:
    return bool(
        entry.get("identity_status") == "eligible"
        and entry.get("publisher_group_kind") == "editorial"
        and entry.get("publisher_registry_status") in _KNOWN_REVIEW_STATES
        and entry.get("source_independence_review_status") in _KNOWN_REVIEW_STATES
        and not ({"press_release", "wire_service"} & set(entry.get("distribution_types") or []))
    )


def _unknown_reason_codes(entry: dict[str, Any]) -> set[str]:
    reasons: set[str] = set()
    if entry.get("identity_status") != "eligible":
        reasons.add("identity_not_eligible")
    if entry.get("publisher_registry_status") == "unreviewed":
        reasons.add("unreviewed_publisher_ownership")
    if entry.get("source_independence_review_status") == "unreviewed":
        reasons.add("source_not_manually_reviewed")
    if entry.get("publisher_group_kind") == "wire_service" or (
        {"press_release", "wire_service"} & set(entry.get("distribution_types") or [])
    ):
        reasons.add("wire_or_press_release_lineage_unresolved")
    if not reasons:
        reasons.add("independence_not_confirmed")
    return reasons
