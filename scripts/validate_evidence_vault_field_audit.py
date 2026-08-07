#!/usr/bin/env python3
"""Verify the committed Evidence Vault field-audit bundle and implementation map."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping


AUDIT_SCHEMA_VERSION = "evidence-vault-field-validation-audit-v1"
TREE_SCHEMA_VERSION = "b3s-evidence-vault-validation-tree-v1"
SELECTION_RULE = (
    "git-tracked pyproject.toml; src/**/*.{py,sql,json}; tests/**/*.py; "
    "docs/evidence_vault_canonical_memory_adr_v{1,2}.md; "
    "fixtures/evidence_vault_field_validation_v1/**; "
    "scripts/*evidence_vault*.py; web/scan_runner.py"
)


class EvidenceVaultFieldAuditError(ValueError):
    """The audit bundle is missing, stale, or internally inconsistent."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvidenceVaultFieldAuditError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise EvidenceVaultFieldAuditError(f"JSON artifact must be an object: {path}")
    return value


def _relative_file(base: Path, raw: Any, *, field: str) -> Path:
    value = str(raw or "").strip()
    if not value:
        raise EvidenceVaultFieldAuditError(f"{field} is required")
    candidate = (base / value).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise EvidenceVaultFieldAuditError(f"{field} escapes its bundle") from exc
    if not candidate.is_file():
        raise EvidenceVaultFieldAuditError(f"missing {field}: {value}")
    return candidate


def _git_tracked(
    repo_root: Path,
    *,
    git_commit: str | None = None,
) -> set[str]:
    command = (
        ["git", "ls-tree", "-r", "--name-only", "-z", git_commit]
        if git_commit is not None
        else ["git", "ls-files", "-z"]
    )
    result = subprocess.run(
        command,
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise EvidenceVaultFieldAuditError("implementation scope requires a Git worktree")
    return {
        raw.decode("utf-8")
        for raw in result.stdout.split(b"\0")
        if raw
    }


def _git_head_and_status(repo_root: Path) -> tuple[str, list[str]]:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0 or status.returncode != 0:
        raise EvidenceVaultFieldAuditError("audit verification requires a Git worktree")
    return head.stdout.strip(), status.stdout.splitlines()


def _audited_implementation_commit(repo_root: Path) -> str:
    relative_tree = (
        "audits/evidence_vault_field_validation_v1/implementation-tree.json"
    )
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", relative_tree],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or len(commit) != 40:
        raise EvidenceVaultFieldAuditError(
            "audited implementation commit cannot be resolved"
        )
    return commit


def _git_blob(repo_root: Path, git_commit: str, relative: str) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{git_commit}:{relative}"],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise EvidenceVaultFieldAuditError(
            f"audited implementation blob is missing: {relative}"
        )
    return result.stdout


def expected_implementation_paths(
    repo_root: Path,
    *,
    git_commit: str | None = None,
) -> set[str]:
    tracked = _git_tracked(repo_root, git_commit=git_commit)
    result: set[str] = set()
    for relative in tracked:
        path = Path(relative)
        if relative == "pyproject.toml":
            result.add(relative)
        elif relative.startswith("src/") and path.suffix in {".py", ".sql", ".json"}:
            result.add(relative)
        elif relative.startswith("tests/") and path.suffix == ".py":
            result.add(relative)
        elif relative in {
            "docs/evidence_vault_canonical_memory_adr_v1.md",
            "docs/evidence_vault_canonical_memory_adr_v2.md",
            "web/scan_runner.py",
        }:
            result.add(relative)
        elif relative.startswith("fixtures/evidence_vault_field_validation_v1/"):
            result.add(relative)
        elif (
            relative.startswith("scripts/")
            and "evidence_vault" in path.name
            and path.suffix == ".py"
        ):
            result.add(relative)
    return result


def _verify_digest(path: Path, record: Mapping[str, Any], *, label: str) -> None:
    if path.stat().st_size != record.get("bytes"):
        raise EvidenceVaultFieldAuditError(f"{label} byte count mismatch: {path}")
    if _sha256(path) != record.get("sha256"):
        raise EvidenceVaultFieldAuditError(f"{label} SHA-256 mismatch: {path}")


def validate_field_audit(
    manifest_path: Path,
    *,
    repo_root: Path | None = None,
    external_root: Path | None = None,
    require_external: bool = False,
    require_clean: bool = False,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    root = (repo_root or Path(__file__).parents[1]).resolve()
    git_head, git_status = _git_head_and_status(root)
    if require_clean and git_status:
        raise EvidenceVaultFieldAuditError(
            "committed landing verification requires a clean Git worktree"
        )
    manifest = _json_object(manifest_path)
    if manifest.get("schema_version") != AUDIT_SCHEMA_VERSION:
        raise EvidenceVaultFieldAuditError("audit manifest schema mismatch")
    if manifest.get("authority") is not False or manifest.get("cutover_authorized") is not False:
        raise EvidenceVaultFieldAuditError("audit manifest crosses the no-authority boundary")

    audit_dir = manifest_path.parent
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise EvidenceVaultFieldAuditError("audit artifact ledger is missing")
    artifact_names: set[str] = set()
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            raise EvidenceVaultFieldAuditError("audit artifact record is invalid")
        name = str(raw.get("file") or "")
        if name in artifact_names:
            raise EvidenceVaultFieldAuditError(f"duplicate audit artifact: {name}")
        artifact_names.add(name)
        path = _relative_file(audit_dir, name, field="artifact file")
        _verify_digest(path, raw, label="audit artifact")

    tree_path = _relative_file(
        audit_dir,
        "implementation-tree.json",
        field="implementation tree",
    )
    if _sha256(tree_path) != manifest.get("implementation_tree_sha256"):
        raise EvidenceVaultFieldAuditError("implementation tree ledger hash mismatch")
    tree = _json_object(tree_path)
    files = tree.get("files")
    if (
        tree.get("schema_version") != TREE_SCHEMA_VERSION
        or tree.get("selection_rule") != SELECTION_RULE
        or not isinstance(files, dict)
        or tree.get("file_count") != len(files)
    ):
        raise EvidenceVaultFieldAuditError("implementation tree contract mismatch")
    audited_commit = _audited_implementation_commit(root)
    expected_paths = expected_implementation_paths(
        root,
        git_commit=audited_commit,
    )
    if set(files) != expected_paths:
        missing = sorted(expected_paths - set(files))
        unexpected = sorted(set(files) - expected_paths)
        raise EvidenceVaultFieldAuditError(
            f"implementation selection mismatch; missing={missing[:5]} unexpected={unexpected[:5]}"
        )
    current_implementation_matches_audit = True
    for relative, expected_hash in files.items():
        audited_blob = _git_blob(root, audited_commit, relative)
        if hashlib.sha256(audited_blob).hexdigest() != expected_hash:
            raise EvidenceVaultFieldAuditError(
                f"audited implementation blob SHA-256 mismatch: {relative}"
            )
        current_path = (root / relative).resolve()
        if (
            not current_path.is_file()
            or _sha256(current_path) != expected_hash
        ):
            current_implementation_matches_audit = False
    fingerprint = hashlib.sha256(
        json.dumps(
            files,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if (
        fingerprint != tree.get("implementation_fingerprint")
        or fingerprint != manifest.get("implementation_fingerprint")
        or tree.get("git_base") != manifest.get("git_base")
    ):
        raise EvidenceVaultFieldAuditError("implementation fingerprint binding mismatch")

    fixture_path = root / "fixtures/evidence_vault_field_validation_v1/manifest.json"
    if _sha256(fixture_path) != manifest.get("fixture_manifest_sha256"):
        raise EvidenceVaultFieldAuditError("fixture manifest binding mismatch")

    external_verified: list[str] = []
    external_unavailable: list[str] = []
    external = manifest.get("external_artifacts") or []
    if not isinstance(external, list):
        raise EvidenceVaultFieldAuditError("external artifact ledger is invalid")
    for raw in external:
        if not isinstance(raw, Mapping):
            raise EvidenceVaultFieldAuditError("external artifact record is invalid")
        declared = Path(str(raw.get("path") or "")).expanduser()
        path = (
            declared
            if declared.is_absolute()
            else (external_root / declared if external_root else declared)
        )
        if not path.is_file():
            external_unavailable.append(str(declared))
            continue
        _verify_digest(path, raw, label="external artifact")
        external_verified.append(str(path))
    if require_external and external_unavailable:
        raise EvidenceVaultFieldAuditError(
            f"external artifacts unavailable: {external_unavailable}"
        )

    return {
        "schema_version": "evidence-vault-field-audit-verification-v1",
        "artifact_count": len(artifact_names),
        "implementation_file_count": len(files),
        "implementation_fingerprint": fingerprint,
        "audited_git_commit": audited_commit,
        "current_implementation_matches_audit": (
            current_implementation_matches_audit
        ),
        "external_verified": external_verified,
        "external_unavailable": external_unavailable,
        "git_head": git_head,
        "git_worktree_clean": not git_status,
        "authority": False,
        "cutover_authorized": False,
        "status": "passed",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).parents[1]
    parser.add_argument(
        "--manifest",
        type=Path,
        default=root / "audits/evidence_vault_field_validation_v1/validation-manifest.json",
    )
    parser.add_argument("--repo-root", type=Path, default=root)
    parser.add_argument("--external-root", type=Path)
    parser.add_argument("--require-external", action="store_true")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow a development worktree instead of committed-landing verification.",
    )
    args = parser.parse_args()
    result = validate_field_audit(
        args.manifest,
        repo_root=args.repo_root,
        external_root=args.external_root,
        require_external=args.require_external,
        require_clean=not args.allow_dirty,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
