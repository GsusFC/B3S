"""SV9 aggregator: pure code, zero LLM, deterministic and testable without mocks.

Applies (baldosas v3.1): score from tile profile (count of lit tiles), pairs
(implicit: each member scores alone, halves never average), x2 multipliers,
statuses to 0, the Magnetism cap, the Brand3 Score, immediate margin, and the
most painful gap. The confidence index is a property of each ComponentResult,
derived from its `sin_evidencia` tiles.
"""

from __future__ import annotations

from src.sv9.models import (
    ComponentResult,
    ESTADO_OK,
    STATUS_NOT_EVALUATED,
    STATUS_SCORED,
    Sv9ScanResult,
    TileVerdict,
)
from src.sv9.rubric import (
    BASE_COMPONENTS,
    COHERENCIA_REVIEW_THRESHOLD,
    COMPONENTS,
    MAGNETISM_CAP_BASE_THRESHOLD,
    MAGNETISM_CAP_VALUE,
    PRESENTATION_ORDER,
    component_max_points,
)
from src.sv9.source_policy import apply_source_policy


def score_from_tile_profile(tile_profile: list[TileVerdict]) -> int:
    """Tile scoring (baldosas v3.1): every lit tile adds one point,
    independently of the others. `no` and `sin_evidencia` both score 0.

    The number is computed by code, never by the model.
    """
    return sum(1 for verdict in tile_profile if verdict.estado == ESTADO_OK)


def base_average(components: dict[str, ComponentResult]) -> float:
    """Mean of the 8 base components, normalized to the 0-10 scale.

    Zero-status components (not detected, not evaluated) count as 0: a brilliant
    phrase on a broken base is a slogan, not magnetism.
    """
    normalized = []
    for key in BASE_COMPONENTS:
        component = components[key]
        score = component.score if component.status == STATUS_SCORED else 0
        normalized.append(score * (10 / COMPONENTS[key]["scale"]))
    return sum(normalized) / len(normalized)


def apply_magnetism_cap(components: dict[str, ComponentResult]) -> tuple[float, bool]:
    """Cap the lit Magnetism tiles at 5 when the base average is below 4/10.

    Mutates the magnetism ComponentResult in place (the cap is a post-process,
    not a prompt rule). Returns (base_avg, capped).
    """
    avg = base_average(components)
    magnetism = components["magnetism"]
    capped = False
    if (
        magnetism.status == STATUS_SCORED
        and avg < MAGNETISM_CAP_BASE_THRESHOLD
        and magnetism.score > MAGNETISM_CAP_VALUE
    ):
        magnetism.score = MAGNETISM_CAP_VALUE
        capped = True
    return avg, capped


def immediate_margin(components: dict[str, ComponentResult], *, magnetism_capped: bool) -> int:
    """Points unlocked by lighting exactly one more tile in each component.

    Not-detected components count (their next tile is the first one). Technical
    failures promise nothing. A capped Magnetism gains nothing from one more tile
    while the base is broken.
    """
    margin = 0
    for key, component in components.items():
        if component.status == STATUS_NOT_EVALUATED:
            continue
        if key == "magnetism" and magnetism_capped:
            continue
        if component.score < COMPONENTS[key]["scale"]:
            margin += COMPONENTS[key]["multiplier"]
    return margin


def most_painful_gap(components: dict[str, ComponentResult]) -> str | None:
    """Component with the widest distance between its weight and its score.

    Technical failures are excluded: a gap must be diagnosis, not an outage.
    Ties break toward the heavier component, then presentation order.
    """
    best_key = None
    best_rank: tuple[int, int, int] | None = None
    for position, key in enumerate(PRESENTATION_ORDER):
        component = components[key]
        if component.status == STATUS_NOT_EVALUATED:
            continue
        gap = component_max_points(key) - component.points
        if gap <= 0:
            continue
        rank = (-gap, -component_max_points(key), position)
        if best_rank is None or rank < best_rank:
            best_rank = rank
            best_key = key
    return best_key


def aggregate(
    components: dict[str, ComponentResult],
    *,
    brand_name: str,
    url: str,
    source_run_id: int | None = None,
) -> Sv9ScanResult:
    """Build the full scan result from per-component evaluations."""
    missing = set(COMPONENTS) - set(components)
    if missing:
        raise ValueError(f"SV9 aggregate requires every component; missing: {sorted(missing)}")

    apply_source_policy(components)
    avg, capped = apply_magnetism_cap(components)
    brand3_score = sum(c.points for c in components.values())
    coherencia = components["coherencia"]
    needs_review = (
        coherencia.status == STATUS_SCORED
        and coherencia.score <= COHERENCIA_REVIEW_THRESHOLD
    )

    return Sv9ScanResult(
        brand_name=brand_name,
        url=url,
        source_run_id=source_run_id,
        components=components,
        brand3_score=brand3_score,
        base_average=round(avg, 2),
        magnetism_capped=capped,
        immediate_margin=immediate_margin(components, magnetism_capped=capped),
        most_painful_gap=most_painful_gap(components),
        needs_review=needs_review,
    )
