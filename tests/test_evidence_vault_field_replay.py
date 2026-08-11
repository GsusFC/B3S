from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.services.evidence_vault_field_replay import (
    EvidenceVaultFieldReplayError,
    validate_evidence_vault_field_replay,
)


FIXTURE_MANIFEST = (
    Path(__file__).parents[1]
    / "fixtures"
    / "evidence_vault_field_validation_v1"
    / "manifest.json"
)
FIXTURE_MANIFEST_SHA256 = (
    "096b611cc6a6708bb17c462f18782a5a22d914857b2e45ec9c54783ddc98c60b"
)
EXPECTED_REPLAY_RESULT_FINGERPRINT = (
    "b47681fe1493d5f7ac8986c5a91890c3219ceae3a36995eed404198d64543fc4"
)


def test_real_brand_normalized_replay_freezes_expected_plan_behavior() -> None:
    assert hashlib.sha256(FIXTURE_MANIFEST.read_bytes()).hexdigest() == (
        FIXTURE_MANIFEST_SHA256
    )
    result = validate_evidence_vault_field_replay(FIXTURE_MANIFEST)

    assert result["result_fingerprint"] == EXPECTED_REPLAY_RESULT_FINGERPRINT
    assert result["source_manifest_sha256"] == FIXTURE_MANIFEST_SHA256
    assert result["gate_status"] == "pass"
    assert result["authority"] is False
    assert result["cutover_authorized"] is False
    assert result["replay_level"] == "normalized_evidence_pack_only"
    assert len(result["result_fingerprint"]) == 64
    cases = {row["case_id"]: row for row in result["cases"]}
    assert set(cases) == {"soccersolver", "causa_prima"}
    assert cases["soccersolver"]["brand_identity"] == "soccersolver.com"
    assert cases["causa_prima"]["brand_identity"] == "causaprima.ai"
    assert cases["soccersolver"]["baseline"] == {
        "context_count": 137,
        "classify_count": 130,
        "operation_plan_fingerprint": (
            "b8f0f3b187b0e5c6e67667d70977631a884b4cfce12cb936d9c45dcd8882fccd"
        ),
    }
    assert [
        row["canonical_impact"]
        for row in cases["soccersolver"]["transitions"]
    ] == ["none", "review_required"]
    assert [
        row["classify_count"]
        for row in cases["soccersolver"]["transitions"]
    ] == [0, 10]
    assert cases["causa_prima"]["baseline"]["classify_count"] == 42
    assert cases["causa_prima"]["transitions"][0]["classify_count"] == 7
    assert all(
        replay["llm_required"] is False
        for case in cases.values()
        for replay in case["identical_replays"]
    )
    assert "raw_acquisition_replay" in result["not_validated"]
    assert "production_cutover" in result["not_validated"]
    downstream = result["downstream_reference"]
    assert downstream["report_chronology"] == [
        "a8ba05137817",
        "5bbeaa6dc58f",
        "76b281740607",
        "c08a4847f063",
        "5d713fcfb53a",
    ]
    assert downstream["accepted_evidence_count"] == 137
    assert downstream["accepted_evidence_occurrence_count"] == 250
    assert downstream["tile_count"] == 80
    assert downstream["current_score"] == downstream["recomputed_current_score"] == 91
    assert downstream["importable_as_v2_review"] is False


def test_replay_rejects_fixture_content_drift(tmp_path: Path) -> None:
    manifest = json.loads(FIXTURE_MANIFEST.read_text())
    source_dir = FIXTURE_MANIFEST.parent
    for case in manifest["cases"]:
        for row in case["observations"]:
            source = source_dir / row["evidence_pack_file"]
            (tmp_path / source.name).write_bytes(source.read_bytes())
    for row in manifest["downstream_reference"]["files"]:
        source = source_dir / row["file"]
        target = tmp_path / row["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    pack_path = tmp_path / manifest["cases"][0]["observations"][0][
        "evidence_pack_file"
    ]
    pack = json.loads(pack_path.read_text())
    pack["evidence"][0]["content"] += " tampered"
    pack_path.write_text(json.dumps(pack))

    with pytest.raises(EvidenceVaultFieldReplayError, match="fixture is invalid"):
        validate_evidence_vault_field_replay(manifest_path)
