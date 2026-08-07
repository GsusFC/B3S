from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

import pytest


_ROOT = Path(__file__).parents[1]
_AUDIT = _ROOT / "audits/evidence_vault_field_validation_v1"
_SCRIPT = _ROOT / "scripts/validate_evidence_vault_field_audit.py"


def _module():
    spec = importlib.util.spec_from_file_location("field_audit_validator", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_field_audit_is_content_addressed_and_non_authoritative() -> None:
    result = _module().validate_field_audit(
        _AUDIT / "validation-manifest.json",
        repo_root=_ROOT,
    )

    assert result["status"] == "passed"
    assert result["artifact_count"] == 26
    assert result["authority"] is False
    assert result["cutover_authorized"] is False
    assert len(result["external_unavailable"]) == 3


def test_field_audit_rejects_tampered_tracked_artifact(tmp_path: Path) -> None:
    copied = tmp_path / "audit"
    shutil.copytree(_AUDIT, copied)
    target = copied / "planner-replay.json"
    target.write_bytes(target.read_bytes() + b" ")

    module = _module()
    with pytest.raises(
        module.EvidenceVaultFieldAuditError,
        match="byte count mismatch",
    ):
        module.validate_field_audit(
            copied / "validation-manifest.json",
            repo_root=_ROOT,
        )


def test_field_audit_enforces_declared_implementation_scope(tmp_path: Path) -> None:
    copied = tmp_path / "audit"
    shutil.copytree(_AUDIT, copied)
    tree_path = copied / "implementation-tree.json"
    tree = json.loads(tree_path.read_text(encoding="utf-8"))
    tree["files"].pop("web/scan_runner.py")
    tree["file_count"] = len(tree["files"])
    tree["implementation_fingerprint"] = hashlib.sha256(
        json.dumps(
            tree["files"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    tree_path.write_text(
        json.dumps(tree, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest_path = copied / "validation-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tree_sha = hashlib.sha256(tree_path.read_bytes()).hexdigest()
    manifest["implementation_tree_sha256"] = tree_sha
    manifest["implementation_fingerprint"] = tree[
        "implementation_fingerprint"
    ]
    for artifact in manifest["artifacts"]:
        if artifact["file"] == "implementation-tree.json":
            artifact["bytes"] = tree_path.stat().st_size
            artifact["sha256"] = tree_sha
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    module = _module()
    with pytest.raises(
        module.EvidenceVaultFieldAuditError,
        match="implementation selection mismatch",
    ):
        module.validate_field_audit(
            manifest_path,
            repo_root=_ROOT,
        )
