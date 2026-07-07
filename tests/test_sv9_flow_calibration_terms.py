from src.sv9_flow.block_detection_worker import BLOCK_DETECTION_POLICY_VERSION, SENSITIVE_BLOCKS
from src.sv9_flow.block_evidence_worker import BLOCK_EVIDENCE_SHORTLIST_VERSION
from src.sv9_flow.calibration_terms import (
    block_detection_policy,
    block_evidence_policy,
    core_purpose_heuristics,
    magnetism_direct_pull_gap_markers,
    magnetism_families,
    provenance,
)


def test_calibration_terms_carry_provenance() -> None:
    assert "overfit" in provenance().lower()
    assert "calibration" in provenance().lower()


def test_policy_versions_match_worker_constants() -> None:
    assert (
        block_detection_policy()["version"]
        == BLOCK_DETECTION_POLICY_VERSION
        == "sv9-flow-block-detection-policy-v6"
    )
    assert (
        block_evidence_policy()["version"]
        == BLOCK_EVIDENCE_SHORTLIST_VERSION
        == "sv9-flow-block-evidence-shortlists-v3"
    )


def test_detection_terms_cover_all_sensitive_blocks() -> None:
    assert SENSITIVE_BLOCKS <= set(block_detection_policy()["support_terms"])


def test_magnetism_families_are_complete() -> None:
    families = magnetism_families()
    assert set(families) == {
        "direct_pull",
        "broad_market",
        "owned_hook",
        "preference",
        "belonging_status",
        "gravity",
    }
    assert "prevención activa de caídas" in families["owned_hook"]
    assert "prevención activa de caídas" in families["direct_pull"]
    assert magnetism_direct_pull_gap_markers()


def test_core_purpose_heuristics_have_required_sections() -> None:
    heuristics = core_purpose_heuristics()
    for key in (
        "generic_category_markers",
        "relies_on_standard_compound",
        "product_bound_terms",
        "beyond_product_terms",
        "action_terms",
        "product_terms",
        "mission_vision_overlap_terms",
        "mission_vision_overlap_guard_terms",
        "strategic_stop_words",
        "token_roots",
    ):
        assert heuristics[key], f"missing or empty section: {key}"
