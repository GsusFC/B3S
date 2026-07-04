"""Export a real Brand3 scan payload into the AutoResearch candidate format."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXPERIMENT_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = EXPERIMENT_ROOT / "candidate"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def get_magnetism_scan(scan_id: int) -> dict[str, Any] | None:
    """Load a legacy magnetism scan without depending on the removed web app."""

    db_path = Path(os.environ.get("BRAND3_DB_PATH", "brand3.sqlite3"))
    if not db_path.is_file():
        return None
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM magnetism_scans WHERE id = ?", (scan_id,)).fetchone()
        except sqlite3.Error:
            return None
    return dict(row) if row is not None else None


def load_scan_source(scan_id: int | None = None, scan_file: Path | None = None) -> dict[str, Any]:
    if scan_file is not None:
        source = load_json(scan_file)
    elif scan_id is not None:
        source = get_magnetism_scan(scan_id)
        if source is None:
            raise FileNotFoundError(f"magnetism scan #{scan_id} was not found")
    else:
        raise ValueError("either scan_id or scan_file must be provided")

    if not isinstance(source, dict):
        raise ValueError("scan source must be a dictionary")
    return source


def normalize_scan_payload(scan_source: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any]
    if isinstance(scan_source.get("payload"), dict):
        payload = dict(scan_source["payload"])
    elif isinstance(scan_source.get("raw_payload"), str):
        raw_payload = json.loads(scan_source["raw_payload"])
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
    else:
        payload = dict(scan_source)

    if not isinstance(payload.get("tldr_brand3"), dict):
        nested_validated = payload.get("analyst_tldr_validated")
        if isinstance(nested_validated, dict) and isinstance(nested_validated.get("tldr_brand3"), dict):
            payload["tldr_brand3"] = nested_validated["tldr_brand3"]

    metadata: dict[str, Any] = {
        "source": "magnetism_scan",
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    for key in ("id", "brand_name", "url", "created_at", "source_run_id", "token", "input_type", "input_value"):
        value = scan_source.get(key)
        if value is not None:
            metadata[key] = value

    return {"metadata": metadata, "payload": payload}


def default_output_path(scan_source: dict[str, Any]) -> Path:
    scan_id = scan_source.get("id")
    brand_name = str(scan_source.get("brand_name") or "scan").strip().lower()
    slug = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in brand_name).strip("-")
    if not slug:
        slug = "scan"
    if scan_id is not None:
        filename = f"{slug}-scan-{scan_id}.json"
    else:
        filename = f"{slug}.json"
    return DEFAULT_OUTPUT_DIR / filename


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a real Brand3 scan as an AutoResearch candidate.")
    parser.add_argument("--scan-id", type=int, default=None)
    parser.add_argument("--scan-file", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    scan_source = load_scan_source(scan_id=args.scan_id, scan_file=args.scan_file)
    candidate = normalize_scan_payload(scan_source)
    output_path = args.output or default_output_path(scan_source)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
