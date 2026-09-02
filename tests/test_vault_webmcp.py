from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"
TEMPLATES = ROOT / "web" / "templates"


FLOC_TOOLS = {
    "b3s_list_brand_analyses",
    "b3s_get_brand_analysis",
    "b3s_read_report_markdown",
    "b3s_get_scan_status",
    "b3s_prepare_scan",
    "b3s_continue_degraded_scan",
    "b3s_cancel_scan",
}
REVIEW_TOOLS = {
    "b3s_list_vault_review_cases",
    "b3s_prepare_vault_review_decision",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_vault_webmcp_is_loaded_only_from_explicit_surfaces() -> None:
    index = _read(TEMPLATES / "index.html.j2")
    review = _read(TEMPLATES / "vault_review.html.j2")

    assert index.count('data-b3s-webmcp-surface="floc"') == 1
    assert index.count("data-b3s-scan-form") == 1
    assert review.count('data-b3s-webmcp-surface="review"') == 1
    assert 'data-b3s-webmcp-domain="{{ review.domain }}"' in review
    assert index.count('/static/vault_webmcp_loader.js') == 1
    assert review.count('/static/vault_webmcp_loader.js') == 1


def test_vault_webmcp_loader_fails_closed_outside_vault() -> None:
    loader = _read(STATIC / "vault_webmcp_loader.js")

    assert 'window.location.origin === "https://b3s-vault.fly.dev"' in loader
    assert 'get("webmcp") === "1"' in loader
    assert 'script.src = "/static/vault_webmcp.js"' in loader
    assert "if (!isVault && !isOptInLocal) return;" in loader


def test_vault_webmcp_contract_keeps_auth_and_final_authority_outside_browser_code() -> None:
    source = _read(STATIC / "vault_webmcp.js")

    assert "navigator.modelContext || document.modelContext" in source
    assert source.count("additionalProperties: false") == len(FLOC_TOOLS | REVIEW_TOOLS)
    assert "closedInput(rawInput" in source
    assert source.count("requiresHumanSubmit: true") == 2
    assert source.count("submitted: false") == 2
    assert 'name: "b3s_start_scan"' not in source
    assert 'name: "b3s_prepare_scan"' in source
    assert 'fetch("/scan"' not in source
    assert "/vault/review/${encodeURIComponent(reviewDomain)}/decisions" not in source
    assert "document.cookie" not in source
    assert "Authorization" not in source
    assert "localStorage" not in source


def test_vault_webmcp_discovery_configs_match_registered_tools() -> None:
    source = _read(STATIC / "vault_webmcp.js")
    floc_config = json.loads(_read(ROOT / "webmcp.e2e.json"))
    review_config = json.loads(_read(ROOT / "webmcp.review.e2e.json"))

    configured_floc = {tool["name"] for tool in floc_config["tools"]}
    configured_review = {tool["name"] for tool in review_config["tools"]}
    assert configured_floc == FLOC_TOOLS
    assert configured_review == REVIEW_TOOLS
    for name in FLOC_TOOLS | REVIEW_TOOLS:
        assert source.count(f'name: "{name}"') == 1

    consequential = {
        tool["name"]
        for tool in floc_config["tools"]
        if tool["risk"] == "consequential"
    }
    assert consequential == {
        "b3s_continue_degraded_scan",
        "b3s_cancel_scan",
    }
    assert all(
        "input" not in tool
        for tool in floc_config["tools"]
        if tool["risk"] == "consequential"
    )

    prepare_scan = next(
        tool for tool in floc_config["tools"] if tool["name"] == "b3s_prepare_scan"
    )
    assert prepare_scan["risk"] == "reversible"
    assert prepare_scan["expectedOutputSubset"] == {
        "prepared": True,
        "submitted": False,
        "requiresHumanSubmit": True,
    }


def test_vault_webmcp_javascript_parses_when_node_is_available() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed in this test environment")

    for filename in ("vault_webmcp_loader.js", "vault_webmcp.js"):
        subprocess.run(
            [node, "--check", str(STATIC / filename)],
            check=True,
            capture_output=True,
            text=True,
        )
