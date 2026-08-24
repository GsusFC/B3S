"""Immutable Social Tiles v1 catalog and scoreless verdict contract."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json

from src.research.social_lab_contracts import SocialObservation


CATALOG_VERSION = "b3s-social-tiles-v1"
SOCIAL_TILES_CATALOG_VERSION = CATALOG_VERSION
CAPTURE_SET_SCHEMA = "b3s-social-tiles-capture-set-v1"
CAPTURE_SET_VERSION = CATALOG_VERSION
CAPTURE_SET_DISCLAIMER = "bounded, non-exhaustive capture; ranges cover admitted observations only"


class SocialTilesContractError(ValueError):
    """Base error for invalid catalog or verdict records."""


class CatalogIntegrityError(SocialTilesContractError):
    """Raised when a catalog is not the exact ordered v1 catalog."""


class CaptureSetIntegrityError(SocialTilesContractError):
    """Raised when a capture-set payload does not match validated observations."""


class ComponentDecodeError(SocialTilesContractError):
    """Raised internally for a malformed component response envelope."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ComponentValidationError(SocialTilesContractError):
    """Raised for malformed local component inputs, never for model output."""


class TileState(str, Enum):
    DEMONSTRATED = "demonstrated"
    CONTRADICTED = "contradicted"
    NOT_OBSERVED = "not_observed"
    NOT_ACQUIRED = "not_acquired"


VerdictState = TileState
VERDICT_STATES = tuple(state.value for state in TileState)
COMPONENT_MODEL_STATES = (TileState.DEMONSTRATED.value, TileState.CONTRADICTED.value, TileState.NOT_OBSERVED.value)
COMPONENT_RESPONSE_KEYS = frozenset({"analysis_json"})
COMPONENT_INNER_KEYS = frozenset({"component_id", "tiles"})
COMPONENT_TILE_KEYS = frozenset({"tile_id", "state", "citations"})

COMPONENT_IDS = (
    "voice_in_action",
    "cross_channel_consistency",
    "listening_themes",
    "response_behavior",
    "reciprocity_dialogue",
    "tension_handling",
)

_TILE_ROWS = (
    (
        "voice_in_action",
        (
            ("ST-VI-01", "distinct voice in official posts", ">=2 official posts"),
            ("ST-VI-02", "voice carries into replies", "official post + brand reply"),
        ),
    ),
    (
        "cross_channel_consistency",
        (
            ("ST-CC-01", "voice remains consistent across channels", "official posts on >=2 platforms"),
            ("ST-CC-02", "core messages do not contradict across channels", "official posts on >=2 platforms"),
        ),
    ),
    (
        "listening_themes",
        (
            ("ST-LT-01", "recurring community themes detectable", ">=2 community responses"),
            ("ST-LT-02", "brand acknowledges recurring themes", ">=2 community responses + linked brand reply"),
        ),
    ),
    (
        "response_behavior",
        (
            ("ST-RB-01", "replies address substance", "linked response/reply pair"),
            ("ST-RB-02", "reply behavior consistent", ">=2 linked pairs"),
        ),
    ),
    (
        "reciprocity_dialogue",
        (
            ("ST-RD-01", "replies enable continuation", "linked pair"),
            ("ST-RD-02", "dialogue continues beyond one brand turn", "locally proven parent depth >=3, both roles"),
        ),
    ),
    (
        "tension_handling",
        (
            ("ST-TH-01", "tension acknowledged", "tension-bearing response + linked brand reply"),
            ("ST-TH-02", "constructive response", "tension-bearing response + linked brand reply"),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class _CitationProofRequirement:
    minimum_records: int
    role_minimums: tuple[tuple[str, int], ...] = ()
    minimum_platforms: int = 0
    platform_role: str | None = None
    minimum_linked_pairs: int = 0
    minimum_parent_depth: int = 0


_CITATION_PROOF_REQUIREMENTS = {
    "ST-VI-01": _CitationProofRequirement(2, (("official_brand_post", 2),)),
    "ST-VI-02": _CitationProofRequirement(2, (("official_brand_post", 1), ("brand_reply", 1))),
    "ST-CC-01": _CitationProofRequirement(2, (("official_brand_post", 2),), 2, "official_brand_post"),
    "ST-CC-02": _CitationProofRequirement(2, (("official_brand_post", 2),), 2, "official_brand_post"),
    "ST-LT-01": _CitationProofRequirement(2, (("community_response", 2),)),
    "ST-LT-02": _CitationProofRequirement(3, (("community_response", 2), ("brand_reply", 1)), 0, None, 1),
    "ST-RB-01": _CitationProofRequirement(2, (("community_response", 1), ("brand_reply", 1)), 0, None, 1),
    "ST-RB-02": _CitationProofRequirement(4, (("community_response", 2), ("brand_reply", 2)), 0, None, 2),
    "ST-RD-01": _CitationProofRequirement(2, (("community_response", 1), ("brand_reply", 1)), 0, None, 1),
    "ST-RD-02": _CitationProofRequirement(3, (("community_response", 1), ("brand_reply", 1)), 0, None, 1, 3),
    "ST-TH-01": _CitationProofRequirement(2, (("community_response", 1), ("brand_reply", 1)), 0, None, 1),
    "ST-TH-02": _CitationProofRequirement(2, (("community_response", 1), ("brand_reply", 1)), 0, None, 1),
}


@dataclass(frozen=True, slots=True)
class TileDefinition:
    tile_id: str
    component_id: str
    description: str
    evidence_requirement: str

    def __post_init__(self) -> None:
        for field_name in ("tile_id", "component_id", "description", "evidence_requirement"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")

    @property
    def id(self) -> str:
        return self.tile_id

    @property
    def criterion(self) -> str:
        return f"{self.description} ({self.evidence_requirement})"

    def to_dict(self) -> dict[str, str]:
        return {
            "tile_id": self.tile_id,
            "component_id": self.component_id,
            "description": self.description,
            "evidence_requirement": self.evidence_requirement,
        }


@dataclass(frozen=True, slots=True)
class ComponentDefinition:
    component_id: str
    tiles: tuple[TileDefinition, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.component_id, str) or not self.component_id.strip():
            raise ValueError("component_id must be a non-empty string")
        object.__setattr__(self, "tiles", tuple(self.tiles))

    @property
    def id(self) -> str:
        return self.component_id

    @property
    def tile_ids(self) -> tuple[str, ...]:
        return tuple(tile.tile_id for tile in self.tiles)

    def to_dict(self) -> dict[str, object]:
        return {"component_id": self.component_id, "tiles": [tile.to_dict() for tile in self.tiles]}


@dataclass(frozen=True, slots=True)
class SocialTilesCatalog:
    version: str
    components: tuple[ComponentDefinition, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "components", tuple(self.components))

    @property
    def component_ids(self) -> tuple[str, ...]:
        return tuple(component.component_id for component in self.components)

    @property
    def tile_ids(self) -> tuple[str, ...]:
        return tuple(tile_id for component in self.components for tile_id in component.tile_ids)

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "components": [component.to_dict() for component in self.components]}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _build_catalog() -> SocialTilesCatalog:
    components = tuple(
        ComponentDefinition(
            component_id=component_id,
            tiles=tuple(
                TileDefinition(
                    tile_id=tile_id,
                    component_id=component_id,
                    description=description,
                    evidence_requirement=requirement,
                )
                for tile_id, description, requirement in rows
            ),
        )
        for component_id, rows in _TILE_ROWS
    )
    return SocialTilesCatalog(version=CATALOG_VERSION, components=components)


CATALOG = _build_catalog()
SOCIAL_TILES_CATALOG = CATALOG
TILE_IDS = CATALOG.tile_ids


def assert_catalog_integrity(catalog: SocialTilesCatalog = CATALOG) -> None:
    """Fail closed unless ``catalog`` exactly matches the ordered v1 contract."""
    if not isinstance(catalog, SocialTilesCatalog) or catalog.version != CATALOG_VERSION:
        raise CatalogIntegrityError("catalog version is unknown")
    if catalog.component_ids != COMPONENT_IDS:
        raise CatalogIntegrityError("catalog components are missing, unknown, or out of order")
    if catalog.tile_ids != TILE_IDS or len(set(catalog.tile_ids)) != len(catalog.tile_ids):
        raise CatalogIntegrityError("catalog tiles are missing, unknown, duplicated, or out of order")
    if set(_CITATION_PROOF_REQUIREMENTS) != set(TILE_IDS):
        raise CatalogIntegrityError("catalog citation proof requirements are missing or unknown")
    for actual_component, expected_component in zip(catalog.components, CATALOG.components):
        if actual_component != expected_component:
            raise CatalogIntegrityError(f"catalog component {actual_component.component_id!r} differs from v1")
        if any(tile.component_id != actual_component.component_id for tile in actual_component.tiles):
            raise CatalogIntegrityError(f"catalog tile linkage is invalid for {actual_component.component_id!r}")


assert_catalog_integrity()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _capture_digest(items: Iterable[Mapping[str, object]]) -> str:
    ordered = sorted((dict(item) for item in items), key=_canonical_json)
    return "sha256:" + sha256(_canonical_json(ordered).encode("utf-8")).hexdigest()


def _validated_capture_observations(observations: Iterable[SocialObservation]) -> tuple[SocialObservation, ...]:
    if isinstance(observations, (str, bytes, Mapping)):
        raise ValueError("observations must be an iterable of validated SocialObservation records")
    try:
        values = tuple(observations)
    except TypeError as exc:
        raise ValueError("observations must be iterable") from exc
    if any(not isinstance(observation, SocialObservation) for observation in values):
        raise ValueError("capture sets accept only validated SocialObservation records")
    return values


def _semantic_capture_item(observation: SocialObservation) -> dict[str, object]:
    provenance = observation.provenance
    return {
        "content_id": observation.content_id,
        "provider": provenance.provider,
        "endpoint_key": provenance.endpoint_key,
        "target_fingerprint": provenance.target_fingerprint,
        "manifest_fingerprint": provenance.manifest_fingerprint,
        "request_fingerprint": provenance.request_fingerprint,
        "page": provenance.page,
        "cursor": provenance.cursor,
        "normalization_version": provenance.normalization_version,
    }


def _receipt_capture_item(observation: SocialObservation) -> dict[str, object]:
    return {"content_id": observation.content_id, "provenance": observation.provenance.to_dict()}


@dataclass(frozen=True, slots=True, init=False)
class SocialCaptureSet:
    schema: str
    version: str
    capture_set_id: str
    receipt_integrity_sha256: str
    observation_count: int
    target_fingerprints: tuple[str, ...]
    platforms: tuple[str, ...]
    observed_published_from: str | None
    observed_published_through: str | None
    fetched_from: str | None
    fetched_through: str | None
    coverage_disclaimer: str

    def __init__(self, observations: Iterable[SocialObservation]) -> None:
        values = _validated_capture_observations(observations)
        semantic_items = [_semantic_capture_item(observation) for observation in values]
        receipt_items = [_receipt_capture_item(observation) for observation in values]
        published = sorted(observation.published_at for observation in values if observation.published_at is not None)
        fetched = sorted(observation.provenance.fetched_at for observation in values)
        fields = {
            "schema": CAPTURE_SET_SCHEMA,
            "version": CAPTURE_SET_VERSION,
            "capture_set_id": _capture_digest(semantic_items),
            "receipt_integrity_sha256": _capture_digest(receipt_items),
            "observation_count": len(values),
            "target_fingerprints": tuple(sorted({observation.provenance.target_fingerprint for observation in values})),
            "platforms": tuple(sorted({observation.platform for observation in values})),
            "observed_published_from": published[0] if published else None,
            "observed_published_through": published[-1] if published else None,
            "fetched_from": fetched[0] if fetched else None,
            "fetched_through": fetched[-1] if fetched else None,
            "coverage_disclaimer": CAPTURE_SET_DISCLAIMER,
        }
        for name, value in fields.items():
            object.__setattr__(self, name, value)

    @classmethod
    def from_observations(cls, observations: Iterable[SocialObservation]) -> "SocialCaptureSet":
        return cls(observations)

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
        observations: Iterable[SocialObservation],
    ) -> "SocialCaptureSet":
        if not isinstance(payload, Mapping):
            raise CaptureSetIntegrityError("capture-set payload must be an object")
        expected = cls(observations)
        if dict(payload) != expected.to_dict():
            raise CaptureSetIntegrityError("capture-set payload does not match recomputed observations")
        return expected

    reconstruct = from_dict

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "version": self.version,
            "capture_set_id": self.capture_set_id,
            "receipt_integrity_sha256": self.receipt_integrity_sha256,
            "observation_count": self.observation_count,
            "target_fingerprints": list(self.target_fingerprints),
            "platforms": list(self.platforms),
            "observed_published_from": self.observed_published_from,
            "observed_published_through": self.observed_published_through,
            "fetched_from": self.fetched_from,
            "fetched_through": self.fetched_through,
            "coverage_disclaimer": self.coverage_disclaimer,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


CaptureSet = SocialCaptureSet


def build_capture_set(observations: Iterable[SocialObservation]) -> SocialCaptureSet:
    return SocialCaptureSet.from_observations(observations)


def reconstruct_capture_set(
    payload: Mapping[str, object], observations: Iterable[SocialObservation]
) -> SocialCaptureSet:
    return SocialCaptureSet.from_dict(payload, observations)


validate_capture_set = reconstruct_capture_set


@dataclass(frozen=True, slots=True)
class TileEligibility:
    tile_id: str
    eligible: bool
    relevant_content_ids: tuple[str, ...] = ()
    reason_code: str | None = None
    failure_stage: str | None = None

    def __post_init__(self) -> None:
        if self.tile_id not in TILE_IDS:
            raise ValueError(f"unknown tile_id: {self.tile_id!r}")
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible must be a boolean")
        if isinstance(self.relevant_content_ids, str):
            raise ValueError("relevant_content_ids must be an iterable of strings")
        try:
            raw_ids = tuple(self.relevant_content_ids)
        except TypeError as exc:
            raise ValueError("relevant_content_ids must be iterable") from exc
        if any(not isinstance(content_id, str) or not content_id for content_id in raw_ids):
            raise ValueError("relevant_content_ids must contain strings")
        ids = tuple(sorted(set(raw_ids)))
        object.__setattr__(self, "relevant_content_ids", ids)
        if self.eligible and (self.reason_code is not None or self.failure_stage is not None):
            raise ValueError("eligible records cannot contain failure metadata")
        if not self.eligible and (
            not isinstance(self.reason_code, str) or not self.reason_code.strip() or self.failure_stage != "eligibility"
        ):
            raise ValueError("ineligible records require reason_code and eligibility failure_stage")

    def to_dict(self) -> dict[str, object]:
        return {
            "tile_id": self.tile_id,
            "eligible": self.eligible,
            "relevant_content_ids": list(self.relevant_content_ids),
            "reason_code": self.reason_code,
            "failure_stage": self.failure_stage,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


def _eligibility_record(
    tile_id: str,
    eligible: bool,
    observations: Iterable[SocialObservation],
    reason_code: str,
) -> TileEligibility:
    return TileEligibility(
        tile_id=tile_id,
        eligible=eligible,
        relevant_content_ids=tuple(observation.content_id for observation in observations),
        reason_code=None if eligible else reason_code,
        failure_stage=None if eligible else "eligibility",
    )


def _eligibility_observations(observations: Iterable[SocialObservation]) -> tuple[SocialObservation, ...]:
    if isinstance(observations, (str, bytes, Mapping)):
        raise ValueError("observations must be an iterable of validated SocialObservation records")
    try:
        values = tuple(observations)
    except TypeError as exc:
        raise ValueError("observations must be iterable") from exc
    if any(not isinstance(observation, SocialObservation) for observation in values):
        raise ValueError("eligibility accepts only validated SocialObservation records")
    values = tuple(item for item in values if item.actor_role != "unclassified")
    external_ids = [observation.external_id for observation in values]
    if len(set(external_ids)) != len(external_ids):
        raise ValueError("duplicate external_id makes local parent linkage ambiguous")
    return tuple(sorted(values, key=lambda item: item.external_id))


ScopedTargetIdentity = tuple[str, str]


def _provenance_target_id(observation: SocialObservation) -> str | None:
    target_id = observation.provenance.linkage_evidence.get("target_id")
    if isinstance(target_id, str) and target_id and target_id == target_id.strip():
        return target_id
    return None


def _target_scopes(observations: tuple[SocialObservation, ...]) -> dict[str, ScopedTargetIdentity | None]:
    target_ids = {_provenance_target_id(observation) for observation in observations}
    target_ids.discard(None)
    target_fingerprints = {observation.provenance.target_fingerprint for observation in observations}
    fallback_fingerprint = next(iter(target_fingerprints)) if len(target_fingerprints) == 1 and not target_ids else None
    return {
        observation.content_id: (
            (target_id, observation.provenance.target_fingerprint)
            if (target_id := _provenance_target_id(observation)) is not None
            else ("", fallback_fingerprint)
            if fallback_fingerprint is not None
            else None
        )
        for observation in observations
    }


def _parent_map(observations: tuple[SocialObservation, ...]) -> dict[str, SocialObservation]:
    scopes = _target_scopes(observations)
    by_scoped_external_id: dict[tuple[str, ScopedTargetIdentity, str], SocialObservation] = {}
    for observation in observations:
        if (scope := scopes[observation.content_id]) is not None:
            by_scoped_external_id[(observation.platform, scope, observation.external_id)] = observation
    parents: dict[str, SocialObservation] = {}
    for observation in observations:
        scope = scopes[observation.content_id]
        if scope is None or observation.parent_external_id is None:
            continue
        parent = by_scoped_external_id.get((observation.platform, scope, observation.parent_external_id))
        if parent is not None:
            parents[observation.content_id] = parent
    return parents


def _parent_chain(
    observation: SocialObservation,
    parent_map: Mapping[str, SocialObservation],
) -> tuple[SocialObservation, ...]:
    chain: list[SocialObservation] = []
    seen: set[str] = set()
    current: SocialObservation | None = observation
    while current is not None:
        if current.content_id in seen:
            return ()
        seen.add(current.content_id)
        chain.append(current)
        current = parent_map.get(current.content_id)
    return tuple(reversed(chain))


def _linked_pairs(
    observations: tuple[SocialObservation, ...],
    parent_map: Mapping[str, SocialObservation],
) -> tuple[tuple[SocialObservation, SocialObservation], ...]:
    pairs = [
        (parent, child)
        for child in observations
        if child.actor_role == "brand_reply"
        and (parent := parent_map.get(child.content_id)) is not None
        and parent.actor_role == "community_response"
    ]
    return tuple(sorted(pairs, key=lambda pair: (pair[0].content_id, pair[1].content_id)))


def evaluate_tile_eligibility(observations: Iterable[SocialObservation]) -> tuple[TileEligibility, ...]:
    """Evaluate only deterministic local prerequisites; provider semantics remain downstream."""
    values = _eligibility_observations(observations)
    parent_map = _parent_map(values)
    official = tuple(observation for observation in values if observation.actor_role == "official_brand_post")
    community = tuple(observation for observation in values if observation.actor_role == "community_response")
    linked_pairs = _linked_pairs(values, parent_map)
    linked_replies = tuple(reply for _, reply in linked_pairs)
    linked_pair_records = tuple(item for pair in linked_pairs for item in pair)
    qualifying_chains = tuple(
        chain
        for observation in values
        if observation.actor_role == "brand_reply"
        for chain in (_parent_chain(observation, parent_map),)
        if len(chain) >= 3
        and any(node.actor_role == "community_response" for node in chain)
        and any(node.actor_role == "brand_reply" for node in chain)
    )
    official_platforms = {observation.platform for observation in official}
    linked_thread_replies = tuple(
        reply
        for reply in linked_replies
        if any(node.actor_role == "official_brand_post" for node in _parent_chain(reply, parent_map))
    )
    vi2_reason = "insufficient_official_posts" if len(official) < 1 else "no_locally_linked_brand_reply_thread"
    lt2_reason = "insufficient_community_responses" if len(community) < 2 else "missing_locally_linked_brand_reply"
    records = {
        "ST-VI-01": _eligibility_record("ST-VI-01", len(official) >= 2, official, "insufficient_official_posts"),
        "ST-VI-02": _eligibility_record(
            "ST-VI-02",
            bool(linked_thread_replies),
            tuple((*official, *linked_thread_replies)),
            vi2_reason,
        ),
        "ST-CC-01": _eligibility_record(
            "ST-CC-01", len(official_platforms) >= 2, official, "insufficient_official_post_platforms"
        ),
        "ST-CC-02": _eligibility_record(
            "ST-CC-02", len(official_platforms) >= 2, official, "insufficient_official_post_platforms"
        ),
        "ST-LT-01": _eligibility_record("ST-LT-01", len(community) >= 2, community, "insufficient_community_responses"),
        "ST-LT-02": _eligibility_record(
            "ST-LT-02", len(community) >= 2 and bool(linked_pairs), (*community, *linked_replies), lt2_reason
        ),
        "ST-RB-01": _eligibility_record(
            "ST-RB-01", bool(linked_pairs), linked_pair_records[:2], "no_locally_linked_response_reply_pair"
        ),
        "ST-RB-02": _eligibility_record(
            "ST-RB-02", len(linked_pairs) >= 2, linked_pair_records, "insufficient_locally_linked_pairs"
        ),
        "ST-RD-01": _eligibility_record(
            "ST-RD-01", bool(linked_pairs), linked_pair_records[:2], "no_locally_linked_response_reply_pair"
        ),
        "ST-RD-02": _eligibility_record(
            "ST-RD-02",
            bool(qualifying_chains),
            qualifying_chains[0] if qualifying_chains else (),
            "insufficient_parent_chain_depth",
        ),
        "ST-TH-01": _eligibility_record(
            "ST-TH-01", bool(linked_pairs), linked_pair_records[:2], "no_locally_linked_response_reply_pair"
        ),
        "ST-TH-02": _eligibility_record(
            "ST-TH-02", bool(linked_pairs), linked_pair_records[:2], "no_locally_linked_response_reply_pair"
        ),
    }
    return tuple(records[tile_id] for tile_id in TILE_IDS)


def validate_tile_citation_proof(
    tile_id: str,
    citations: Iterable[str],
    observations: Iterable[SocialObservation],
) -> None:
    """Fail closed unless cited evidence satisfies the catalog's local proof shape."""
    if tile_id not in TILE_IDS or isinstance(citations, (str, bytes, Mapping)):
        raise ComponentValidationError("citation_proof_requirements_not_met")
    try:
        citation_ids = tuple(citations)
    except TypeError as exc:
        raise ComponentValidationError("citation_proof_requirements_not_met") from exc
    values = _eligibility_observations(observations)
    by_content_id = {observation.content_id: observation for observation in values}
    eligibility = {record.tile_id: record for record in evaluate_tile_eligibility(values)}[tile_id]
    if (
        len(by_content_id) != len(values)
        or any(not isinstance(citation, str) or not citation for citation in citation_ids)
        or len(citation_ids) != len(set(citation_ids))
        or not eligibility.eligible
        or not set(citation_ids).issubset(eligibility.relevant_content_ids)
    ):
        raise ComponentValidationError("citation_proof_requirements_not_met")
    cited = tuple(by_content_id[citation] for citation in citation_ids)
    requirement = _CITATION_PROOF_REQUIREMENTS[tile_id]
    if len(cited) < requirement.minimum_records or any(
        sum(observation.actor_role == role for observation in cited) < minimum
        for role, minimum in requirement.role_minimums
    ):
        raise ComponentValidationError("citation_proof_requirements_not_met")
    platforms = {
        observation.platform
        for observation in cited
        if requirement.platform_role is None or observation.actor_role == requirement.platform_role
    }
    if len(platforms) < requirement.minimum_platforms:
        raise ComponentValidationError("citation_proof_requirements_not_met")
    parent_map = _parent_map(values)
    cited_ids = set(citation_ids)
    cited_pairs = tuple(
        pair
        for pair in _linked_pairs(values, parent_map)
        if pair[0].content_id in cited_ids and pair[1].content_id in cited_ids
    )
    if len(cited_pairs) < requirement.minimum_linked_pairs:
        raise ComponentValidationError("citation_proof_requirements_not_met")
    if requirement.minimum_parent_depth and not any(
        len(chain) >= requirement.minimum_parent_depth and all(node.content_id in cited_ids for node in chain)
        for observation in cited
        if observation.actor_role == "brand_reply"
        for chain in (_parent_chain(observation, parent_map),)
    ):
        raise ComponentValidationError("citation_proof_requirements_not_met")


def _component_definition(component_id: str) -> ComponentDefinition:
    if component_id not in COMPONENT_IDS:
        raise ValueError(f"unknown component_id: {component_id!r}")
    return next(component for component in CATALOG.components if component.component_id == component_id)


def _component_eligibility(
    component_id: str,
    eligibility: Iterable[TileEligibility],
) -> dict[str, TileEligibility]:
    component = _component_definition(component_id)
    records = tuple(eligibility)
    if any(not isinstance(record, TileEligibility) for record in records):
        raise ValueError("eligibility must contain TileEligibility records")
    by_id: dict[str, TileEligibility] = {}
    for record in records:
        if record.tile_id in by_id:
            raise ValueError(f"duplicate eligibility for {record.tile_id!r}")
        by_id[record.tile_id] = record
    missing = [tile.tile_id for tile in component.tiles if tile.tile_id not in by_id]
    if missing:
        raise ValueError(f"eligibility is missing component tiles: {missing}")
    return {tile.tile_id: by_id[tile.tile_id] for tile in component.tiles}


def build_component_prompt(
    component_id: str,
    observations: Iterable[SocialObservation],
    eligibility: Iterable[TileEligibility],
) -> dict[str, object]:
    """Build the provider packet for one component without acquisition metadata."""
    component = _component_definition(component_id)
    values = _eligibility_observations(observations)
    by_content_id = {observation.content_id: observation for observation in values}
    if len(by_content_id) != len(values):
        raise ValueError("duplicate content_id makes component prompt ambiguous")
    records = _component_eligibility(component_id, eligibility)
    eligible_tiles = [tile for tile in component.tiles if records[tile.tile_id].eligible]
    relevant_ids = {content_id for tile in eligible_tiles for content_id in records[tile.tile_id].relevant_content_ids}
    if not relevant_ids.issubset(by_content_id):
        raise ValueError("eligibility cites content absent from supplied observations")
    return {
        "component_id": component.component_id,
        "tiles": [
            {
                "tile_id": tile.tile_id,
                "description": tile.description,
                "evidence_requirement": tile.evidence_requirement,
            }
            for tile in eligible_tiles
        ],
        "observations": [
            {
                "content_id": observation.content_id,
                "text": observation.text,
                "actor_role": observation.actor_role,
                "platform": observation.platform,
                "parent_external_id": observation.parent_external_id,
            }
            for observation in sorted(
                (by_content_id[content_id] for content_id in relevant_ids), key=lambda item: item.content_id
            )
        ],
    }


def component_response_schema() -> dict[str, object]:
    return {
        "type": "object",
        "required": ["analysis_json"],
        "properties": {"analysis_json": {"type": "string"}},
        "additionalProperties": False,
    }


def _component_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ComponentDecodeError("duplicate_json_keys")
        result[key] = value
    return result


def _reject_component_constant(_: str) -> None:
    raise ComponentDecodeError("non_json_constant")


def decode_component_response(raw: object) -> Mapping[str, object]:
    """Decode the exact transport envelope and strict inner JSON object."""
    if isinstance(raw, str):
        try:
            envelope = json.loads(
                raw,
                object_pairs_hook=_component_json_pairs,
                parse_constant=_reject_component_constant,
            )
        except ComponentDecodeError:
            raise
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
            raise ComponentDecodeError("malformed_envelope") from None
    elif isinstance(raw, Mapping):
        envelope = dict(raw)
    else:
        raise ComponentDecodeError("malformed_envelope")
    if not isinstance(envelope, Mapping) or set(envelope) != COMPONENT_RESPONSE_KEYS:
        raise ComponentDecodeError("malformed_envelope")
    encoded = envelope.get("analysis_json")
    if not isinstance(encoded, str) or not encoded.strip():
        raise ComponentDecodeError("malformed_envelope")
    try:
        decoded = json.loads(
            encoded,
            object_pairs_hook=_component_json_pairs,
            parse_constant=_reject_component_constant,
        )
    except ComponentDecodeError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise ComponentDecodeError("malformed_inner_json") from None
    if not isinstance(decoded, Mapping):
        raise ComponentDecodeError("inner_not_object")
    return decoded


@dataclass(frozen=True, slots=True)
class ComponentValidationResult:
    component_id: str
    verdicts: tuple[TileVerdict, ...]

    def __post_init__(self) -> None:
        _component_definition(self.component_id)
        verdicts = tuple(self.verdicts)
        if any(not isinstance(verdict, TileVerdict) for verdict in verdicts):
            raise ValueError("verdicts must contain TileVerdict records")
        object.__setattr__(self, "verdicts", verdicts)

    def to_dict(self) -> dict[str, object]:
        return {"component_id": self.component_id, "verdicts": [verdict.to_dict() for verdict in self.verdicts]}

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())


def _component_failure_result(
    component_id: str,
    eligible_tiles: Iterable[TileDefinition],
    reason_code: str,
    failure_stage: str,
) -> ComponentValidationResult:
    return ComponentValidationResult(
        component_id,
        tuple(
            TileVerdict(tile.tile_id, TileState.NOT_ACQUIRED, reason_code=reason_code, failure_stage=failure_stage)
            for tile in eligible_tiles
        ),
    )


def _validated_model_tile(
    tile: Mapping[str, object],
    eligibility: TileEligibility,
    observations: tuple[SocialObservation, ...],
) -> TileVerdict:
    if set(tile) != COMPONENT_TILE_KEYS:
        raise ComponentValidationError("invalid_tile_shape")
    state = tile.get("state")
    if state == TileState.NOT_ACQUIRED.value:
        raise ComponentValidationError("not_acquired_forbidden")
    if state not in COMPONENT_MODEL_STATES:
        raise ComponentValidationError("invalid_state")
    citations = tile.get("citations")
    if not isinstance(citations, list) or any(not isinstance(citation, str) for citation in citations):
        raise ComponentValidationError("invalid_citations")
    if len(citations) != len(set(citations)):
        raise ComponentValidationError("duplicate_citations")
    if state == TileState.NOT_OBSERVED.value and citations:
        raise ComponentValidationError("not_observed_requires_empty_citations")
    if state in {TileState.DEMONSTRATED.value, TileState.CONTRADICTED.value} and not citations:
        raise ComponentValidationError("demonstrated_or_contradicted_requires_citations")
    if not set(citations).issubset(set(eligibility.relevant_content_ids)):
        raise ComponentValidationError("citation_outside_relevant_content")
    if state in {TileState.DEMONSTRATED.value, TileState.CONTRADICTED.value}:
        validate_tile_citation_proof(str(tile["tile_id"]), citations, observations)
    return TileVerdict(
        tile_id=str(tile["tile_id"]),
        state=TileState(state),
        citations=tuple(sorted(citations)),
    )


def validate_component_result(
    component_id: str,
    raw_response: object,
    observations: Iterable[SocialObservation],
    eligibility: Iterable[TileEligibility],
) -> ComponentValidationResult:
    """Validate one component while isolating each eligible tile's failure."""
    component = _component_definition(component_id)
    values = _eligibility_observations(observations)
    records = _component_eligibility(component_id, eligibility)
    expected = _component_eligibility(component_id, evaluate_tile_eligibility(values))
    if records != expected:
        raise ValueError("eligibility does not match validated observations")
    eligible_tiles = [tile for tile in component.tiles if records[tile.tile_id].eligible]
    try:
        decoded = decode_component_response(raw_response)
    except ComponentDecodeError as error:
        return _component_failure_result(component_id, eligible_tiles, error.reason_code, "component_decode")
    if set(decoded) != COMPONENT_INNER_KEYS:
        return _component_failure_result(
            component_id, eligible_tiles, "invalid_component_shape", "component_validation"
        )
    if decoded.get("component_id") != component_id:
        return _component_failure_result(component_id, eligible_tiles, "wrong_component_id", "component_validation")
    raw_tiles = decoded.get("tiles")
    if not isinstance(raw_tiles, list):
        return _component_failure_result(
            component_id, eligible_tiles, "invalid_component_shape", "component_validation"
        )
    by_tile_id: dict[str, Mapping[str, object]] = {}
    duplicates: set[str] = set()
    eligible_ids = {tile.tile_id for tile in eligible_tiles}
    for raw_tile in raw_tiles:
        if not isinstance(raw_tile, Mapping) or not isinstance(raw_tile.get("tile_id"), str):
            continue
        tile_id = raw_tile["tile_id"]
        if tile_id not in eligible_ids:
            continue
        if tile_id in by_tile_id:
            duplicates.add(tile_id)
        else:
            by_tile_id[tile_id] = raw_tile
    verdicts: list[TileVerdict] = []
    for tile in eligible_tiles:
        record = records[tile.tile_id]
        try:
            if tile.tile_id in duplicates:
                raise ComponentValidationError("duplicate_tile_id")
            if tile.tile_id not in by_tile_id:
                raise ComponentValidationError("missing_tile")
            verdicts.append(_validated_model_tile(by_tile_id[tile.tile_id], record, values))
        except ComponentValidationError as error:
            verdicts.append(
                TileVerdict(
                    tile.tile_id,
                    TileState.NOT_ACQUIRED,
                    reason_code=error.args[0],
                    failure_stage="component_validation",
                )
            )
    return ComponentValidationResult(component_id, tuple(verdicts))


def synthesize_not_acquired_verdict(eligibility: TileEligibility) -> TileVerdict:
    if not isinstance(eligibility, TileEligibility):
        raise TypeError("eligibility must be a TileEligibility")
    if eligibility.eligible:
        raise ValueError("eligible tiles cannot be synthesized as not_acquired")
    return TileVerdict(
        tile_id=eligibility.tile_id,
        state=TileState.NOT_ACQUIRED,
        reason_code=eligibility.reason_code,
        failure_stage=eligibility.failure_stage,
    )


@dataclass(frozen=True, slots=True)
class TileVerdict:
    tile_id: str
    state: TileState
    citations: tuple[str, ...] = ()
    reason_code: str | None = None
    failure_stage: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tile_id, str) or self.tile_id not in TILE_IDS:
            raise ValueError(f"unknown tile_id: {self.tile_id!r}")
        try:
            state = TileState(self.state)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"unknown verdict state: {self.state!r}") from exc
        object.__setattr__(self, "state", state)
        if isinstance(self.citations, str) or self.citations is None:
            raise ValueError("citations must be an iterable of non-empty strings")
        citations = tuple(self.citations)
        if any(not isinstance(citation, str) or not citation.strip() for citation in citations):
            raise ValueError("citations must contain only non-empty strings")
        object.__setattr__(self, "citations", citations)
        for name in ("reason_code", "failure_stage"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string when supplied")
        has_failure_metadata = self.reason_code is not None or self.failure_stage is not None
        if state in (TileState.DEMONSTRATED, TileState.CONTRADICTED) and (not citations or has_failure_metadata):
            raise ValueError(f"{state.value} requires citations and no failure metadata")
        if state is TileState.NOT_OBSERVED and (citations or has_failure_metadata):
            raise ValueError("not_observed requires empty citations and no failure metadata")
        if state is TileState.NOT_ACQUIRED and (citations or self.reason_code is None or self.failure_stage is None):
            raise ValueError("not_acquired requires empty citations, reason_code, and failure_stage")

    def to_dict(self) -> dict[str, object]:
        return {
            "tile_id": self.tile_id,
            "state": self.state.value,
            "citations": list(self.citations),
            "reason_code": self.reason_code,
            "failure_stage": self.failure_stage,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def serialize_verdict(verdict: TileVerdict) -> str:
    if not isinstance(verdict, TileVerdict):
        raise TypeError("verdict must be a TileVerdict")
    return verdict.to_json()


__all__ = [
    "CATALOG",
    "CATALOG_VERSION",
    "CAPTURE_SET_DISCLAIMER",
    "CAPTURE_SET_SCHEMA",
    "CAPTURE_SET_VERSION",
    "CaptureSet",
    "CaptureSetIntegrityError",
    "COMPONENT_IDS",
    "CatalogIntegrityError",
    "ComponentDefinition",
    "TileEligibility",
    "SOCIAL_TILES_CATALOG",
    "SOCIAL_TILES_CATALOG_VERSION",
    "SocialTilesCatalog",
    "SocialTilesContractError",
    "TILE_IDS",
    "TileDefinition",
    "TileState",
    "TileVerdict",
    "VERDICT_STATES",
    "VerdictState",
    "assert_catalog_integrity",
    "build_capture_set",
    "evaluate_tile_eligibility",
    "reconstruct_capture_set",
    "serialize_verdict",
    "synthesize_not_acquired_verdict",
    "validate_tile_citation_proof",
    "validate_capture_set",
]
__all__ += [
    "ComponentDecodeError",
    "ComponentValidationError",
    "ComponentValidationResult",
    "build_component_prompt",
    "component_response_schema",
    "decode_component_response",
    "validate_component_result",
]
