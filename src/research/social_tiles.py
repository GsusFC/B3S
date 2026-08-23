"""Immutable Social Tiles v1 catalog and scoreless verdict contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json


CATALOG_VERSION = "b3s-social-tiles-v1"
SOCIAL_TILES_CATALOG_VERSION = CATALOG_VERSION


class SocialTilesContractError(ValueError):
    """Base error for invalid catalog or verdict records."""


class CatalogIntegrityError(SocialTilesContractError):
    """Raised when a catalog is not the exact ordered v1 catalog."""


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
    "serialize_verdict",
]
