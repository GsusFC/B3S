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


class TileState(str, Enum):
    DEMONSTRATED = "demonstrated"
    CONTRADICTED = "contradicted"
    NOT_OBSERVED = "not_observed"
    NOT_ACQUIRED = "not_acquired"


VerdictState = TileState
VERDICT_STATES = tuple(state.value for state in TileState)

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
        if state is TileState.NOT_ACQUIRED and (
            citations or self.reason_code is None or self.failure_stage is None
        ):
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
    "reconstruct_capture_set",
    "serialize_verdict",
    "validate_capture_set",
]
