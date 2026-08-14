from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github/workflows/pr71-sv9-shadow-automation.yml"


def _source() -> str:
    return WORKFLOW.read_text(encoding="utf-8")




def _receipt_validator_source() -> str:
    source = _source()
    marker = '          python - "$receipt" "$GITHUB_STEP_SUMMARY" "$GITHUB_EVENT_NAME" "$GITHUB_RUN_ATTEMPT" <<\'PY\'\n'
    start = source.index(marker) + len(marker)
    end = source.index("\n          PY", start)
    return textwrap.dedent(source[start:end])


def _run_receipt_validator(
    tmp_path: Path,
    payload: dict,
    *,
    event_name: str = "schedule",
    run_attempt: str = "1",
) -> subprocess.CompletedProcess[str]:
    receipt = tmp_path / "receipt.json"
    summary = tmp_path / "summary.md"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _receipt_validator_source(),
            str(receipt),
            str(summary),
            event_name,
            run_attempt,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_scheduler_is_default_branch_only_and_strictly_bounded() -> None:
    source = _source()

    assert 'cron: "17 * * * *"' in source
    assert "workflow_dispatch:" in source
    assert "pull_request:" not in source
    assert "github.repository == 'GsusFC/B3S'" in source
    assert "github.ref == 'refs/heads/main'" in source
    assert "cancel-in-progress: false" in source
    assert "timeout-minutes: 10" in source
    assert 'mode_args=(--dry-run)' in source
    assert (
        'if [[ "$GITHUB_EVENT_NAME" == "schedule" '
        '&& "$GITHUB_RUN_ATTEMPT" == "1" ]]' in source
    )
    assert 'mode_args=(--append)' in source
    assert "--limit 1" in source
    assert "len(items) > 1" in source


def test_scheduler_uses_only_the_isolated_writer_secret() -> None:
    source = _source()

    secret = "secrets.B3S_PR71_SV9_SHADOW_WRITER_DATABASE_URL"
    assert source.count(secret) == 1
    assert "name: pr71-sv9-shadow-automation" in source
    assert "B3S_DATABASE_URL" not in source
    assert "B3S_MIGRATION_DATABASE_URL" not in source
    assert "FLY_API_TOKEN" not in source
    assert "id-token:" not in source
    assert "flyctl" not in source
    assert "fly deploy" not in source


def test_scheduler_pins_actions_and_never_logs_work_item_identity() -> None:
    source = _source()

    assert (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
        in source
    )
    assert (
        "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
        in source
    )
    assert "persist-credentials: false" in source
    assert 'python-version: "3.11.14"' in source
    assert "--require-hashes" in source
    assert "--only-binary=:all:" in source
    assert "requirements-pr71-sv9-shadow.txt" in source
    assert "pip install --disable-pip-version-check ." not in source
    assert 'summary.write(f"- processed: {len(items)}\\n")' in source
    assert 'summary.write(f"- outcome: {outcome}\\n")' in source
    assert 'summary.write(f"- domain:' not in source
    assert 'summary.write(f"- operational_packet_fingerprint:' not in source
    assert 'cat "$receipt"' not in source
    assert "set -euo pipefail" in source
    assert "umask 077" in source
    assert "trap 'rm -rf \"$receipt_dir\"' EXIT" in source


def test_scheduler_validates_the_exact_sanitized_receipt_contract() -> None:
    source = _source()

    for field in (
        '"schema_version"',
        '"mode"',
        '"limit"',
        '"discovered_count"',
        '"processed_count"',
        '"authority"',
        '"production_runtime_effect"',
        '"scanner_runtime_effect"',
        '"work_items"',
    ):
        assert field in source
    assert "unexpected shadow automation receipt shape" in source
    assert "unsafe shadow automation receipt" in source
    assert "unexpected shadow work-item receipt shape" in source

def _receipt(*, items: list[dict] | None = None) -> dict:
    work_items = [] if items is None else items
    return {
        "schema_version": (
            "evidence-vault-operational-sv9-shadow-work-items-admin-run-v1"
        ),
        "mode": "append",
        "limit": 1,
        "discovered_count": len(work_items),
        "processed_count": len(work_items),
        "authority": False,
        "production_runtime_effect": False,
        "scanner_runtime_effect": False,
        "work_items": work_items,
    }


def test_scheduler_receipt_validator_logs_only_safe_summary(tmp_path: Path) -> None:
    item = {
        "domain": "private.example",
        "operational_packet_fingerprint": "a" * 64,
        "expected_parent_canonical_memory_version": "b" * 64,
        "outcome": "appended",
        "assessment_status": "available",
        "replayed": False,
    }
    result = _run_receipt_validator(tmp_path, _receipt(items=[item]))

    assert result.returncode == 0
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "processed: 1" in summary
    assert "outcome: appended" in summary
    assert "authority: false" in summary
    assert "private.example" not in summary
    assert "a" * 64 not in summary


def test_scheduler_receipt_validator_fails_closed_on_extra_data(
    tmp_path: Path,
) -> None:
    payload = _receipt()
    payload["database_url"] = "do-not-log"

    result = _run_receipt_validator(tmp_path, payload)

    assert result.returncode != 0
    assert "unexpected shadow automation receipt shape" in result.stderr
    assert not (tmp_path / "summary.md").exists()

def test_manual_dispatch_receipt_is_dry_run_only(tmp_path: Path) -> None:
    item = {
        "domain": "private.example",
        "operational_packet_fingerprint": "a" * 64,
        "expected_parent_canonical_memory_version": "b" * 64,
        "outcome": "dry_run",
        "assessment_status": "available",
        "replayed": False,
    }
    payload = _receipt(items=[item])
    payload["mode"] = "dry_run"

    result = _run_receipt_validator(
        tmp_path,
        payload,
        event_name="workflow_dispatch",
    )

    assert result.returncode == 0
    summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "outcome: dry_run" in summary
    assert "private.example" not in summary


def test_scheduler_dependency_lock_is_minimal_and_hash_verified() -> None:
    requirement = (PROJECT_ROOT / "requirements-pr71-sv9-shadow.in").read_text(
        encoding="utf-8"
    )
    lock = (PROJECT_ROOT / "requirements-pr71-sv9-shadow.txt").read_text(
        encoding="utf-8"
    )

    assert requirement == "psycopg[binary]==3.2.10\n"
    assert "psycopg==3.2.10" in lock
    assert "psycopg-binary==3.2.10" in lock
    assert lock.count("==3.2.10") == 2
    assert "--hash=sha256:" in lock
    assert "http://" not in lock
    assert "https://" not in lock


def test_scheduled_rerun_receipt_is_dry_run_only(tmp_path: Path) -> None:
    payload = _receipt(
        items=[
            {
                "domain": "private.example",
                "operational_packet_fingerprint": "a" * 64,
                "expected_parent_canonical_memory_version": "b" * 64,
                "outcome": "dry_run",
                "assessment_status": "available",
                "replayed": False,
            }
        ]
    )
    payload["mode"] = "dry_run"

    result = _run_receipt_validator(
        tmp_path,
        payload,
        event_name="schedule",
        run_attempt="2",
    )

    assert result.returncode == 0
    assert "outcome: dry_run" in (tmp_path / "summary.md").read_text(
        encoding="utf-8"
    )


def test_scheduler_receipt_validator_rejects_boolean_counts(tmp_path: Path) -> None:
    payload = _receipt()
    payload["limit"] = True

    result = _run_receipt_validator(tmp_path, payload)

    assert result.returncode != 0
    assert "invalid shadow automation numeric field" in result.stderr
    assert not (tmp_path / "summary.md").exists()
