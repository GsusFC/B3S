from __future__ import annotations

import base64
import importlib
import json
from dataclasses import dataclass

from src.visual_signature.types import VisualAcquisitionResult, VisualSignatureInput
from src.visual_signature.vision import multimodal_analyzer


@dataclass
class FixtureAdapter:
    name = "custom"

    def acquire(self, input_data: VisualSignatureInput) -> VisualAcquisitionResult:
        return VisualAcquisitionResult(
            adapter="custom",
            requested_url=input_data.website_url,
            final_url=input_data.website_url,
            status_code=200,
            rendered_html="<html><body><h1>Example Brand</h1></body></html>",
            raw_html="<html><body><h1>Example Brand</h1></body></html>",
            markdown="# Example Brand",
            acquired_at="2026-05-20T00:00:00",
        )


def test_analyze_visual_semantics_missing_screenshot_returns_stable_fallback():
    result = multimodal_analyzer.analyze_visual_semantics(
        screenshot_path="/tmp/not-a-real-brand3-screenshot.png",
        brand_name="Example Brand",
    )

    assert result == multimodal_analyzer.fallback_semantics("screenshot_file_not_found")
    assert set(result) == {"status", "model", "prompt_version", "fallback_used", "error_type", "audit", "data"}
    assert result["data"]["aesthetic_style"] == "not_detected"
    assert result["data"]["visual_polish_score"] is None
    assert result["data"]["observed_strengths"] == []


def test_build_multimodal_payload_uses_openai_compatible_image_url_block():
    payload = multimodal_analyzer.build_multimodal_payload(
        encoded_image="ZmFrZS1pbWFnZQ==",
        mime_type="image/png",
        brand_name="Example Brand",
    )

    content = payload["messages"][0]["content"]
    assert payload["model"] == multimodal_analyzer.BRAND3_VISUAL_SIGNATURE_MODEL
    assert content[0]["type"] == "text"
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,ZmFrZS1pbWFnZQ=="},
    }
    assert payload["response_format"] == {"type": "json_object"}


def test_multimodal_atlas_prompt_separates_first_viewport_from_page_sections():
    payload = multimodal_analyzer.build_multimodal_payload(
        encoded_image="ZmFrZS1pbWFnZQ==",
        mime_type="image/png",
        brand_name="Example Brand",
        analysis_scope="labeled_section_atlas",
    )

    prompt = payload["messages"][0]["content"][0]["text"]
    assert "FIRST VIEWPORT is the only basis for first impression" in prompt
    assert "SECTION panels are additional whole-page evidence" in prompt
    assert "gray gutters are audit scaffolding" in prompt


def test_analyze_visual_semantics_encodes_image_and_normalizes_success(tmp_path, monkeypatch):
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"brand3 image bytes")
    captured: dict[str, object] = {}

    def fake_http_call(*, url, payload, headers, timeout_seconds):
        captured["url"] = url
        captured["payload"] = json.loads(payload.decode("utf-8"))
        captured["headers"] = headers
        captured["timeout_seconds"] = timeout_seconds
        return "ok", json.dumps({
            "aesthetic_style": "minimalist SaaS",
            "visual_mood": "high-trust",
            "visual_polish": {
                "score": 11,
                "reasoning": "Strong hierarchy and restrained palette.",
            },
            "visual_coherence": "The layout supports a coherent product identity.",
            "brand_distinctiveness": "medium",
            "category_fit": "Strong fit for a B2B SaaS homepage.",
            "copy_visual_alignment": "Visible copy and interface cues are aligned.",
            "logo_prominence": "clear",
            "hierarchy_clarity": "clear",
            "cta_salience": "clear",
            "trust_signal_presence": "partial",
            "first_impression_summary": "Polished, product-first and trustworthy.",
            "observed_strengths": ["clear hierarchy", "restrained palette"],
            "observed_risks": ["limited differentiation"],
            "notable_absences": ["no strong social proof visible"],
        })

    monkeypatch.setattr(multimodal_analyzer, "BRAND3_LLM_API_KEY", "test-key")
    monkeypatch.setattr(multimodal_analyzer, "_run_llm_http_call", fake_http_call)

    result = multimodal_analyzer.analyze_visual_semantics(
        screenshot_path=str(screenshot),
        brand_name="Example Brand",
    )

    image_url = captured["payload"]["messages"][0]["content"][1]["image_url"]["url"]
    assert image_url == "data:image/png;base64," + base64.b64encode(b"brand3 image bytes").decode("utf-8")
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert result["status"] == "detected"
    assert result["fallback_used"] is False
    assert result["error_type"] is None
    assert result["audit"]["analysis_scope"] == "single_capture"
    assert result["data"] == {
        "aesthetic_style": "minimalist SaaS",
        "visual_mood": "high-trust",
        "visual_polish_score": 10,
        "visual_polish_rationale": "Strong hierarchy and restrained palette.",
        "visual_coherence": "The layout supports a coherent product identity.",
        "brand_distinctiveness": "medium",
        "category_fit": "Strong fit for a B2B SaaS homepage.",
        "copy_visual_alignment": "Visible copy and interface cues are aligned.",
        "logo_prominence": "clear",
        "hierarchy_clarity": "clear",
        "cta_salience": "clear",
        "trust_signal_presence": "partial",
        "first_impression_summary": "Polished, product-first and trustworthy.",
        "observed_strengths": ["clear hierarchy", "restrained palette"],
        "observed_risks": ["limited differentiation"],
        "notable_absences": ["no strong social proof visible"],
    }


def test_analyze_visual_semantics_http_failure_returns_neutral_fallback(tmp_path, monkeypatch):
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"brand3 image bytes")

    monkeypatch.setattr(multimodal_analyzer, "BRAND3_LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        multimodal_analyzer,
        "_run_llm_http_call",
        lambda **_kwargs: ("error", "HTTP 429: quota"),
    )

    result = multimodal_analyzer.analyze_visual_semantics(
        screenshot_path=str(screenshot),
        brand_name="Example Brand",
    )

    assert result == multimodal_analyzer.fallback_semantics("llm_error")


def test_analyze_visual_semantics_invalid_json_returns_neutral_fallback(tmp_path, monkeypatch):
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"brand3 image bytes")

    monkeypatch.setattr(multimodal_analyzer, "BRAND3_LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        multimodal_analyzer,
        "_run_llm_http_call",
        lambda **_kwargs: ("ok", "not json"),
    )

    result = multimodal_analyzer.analyze_visual_semantics(
        screenshot_path=str(screenshot),
        brand_name="Example Brand",
    )

    assert result == multimodal_analyzer.fallback_semantics("json_parse_error")


def test_extract_visual_signature_includes_stable_semantics_without_local_screenshot(monkeypatch):
    extract_module = importlib.import_module("src.visual_signature.extract_visual_signature")
    captured: dict[str, object] = {}

    def fake_analyze_visual_semantics(*, screenshot_path, brand_name):
        captured["screenshot_path"] = screenshot_path
        captured["brand_name"] = brand_name
        return multimodal_analyzer.fallback_semantics("screenshot_path_missing")

    monkeypatch.setattr(extract_module, "analyze_visual_semantics", fake_analyze_visual_semantics)

    result = extract_module.extract_visual_signature(
        brand_name="Example Brand",
        website_url="https://example.com",
        screenshot_payload={"path": "/tmp/nonexistent-brand3-screenshot.png"},
        adapter=FixtureAdapter(),
    )

    assert captured == {"screenshot_path": None, "brand_name": "Example Brand"}
    assert result["semantics"] == multimodal_analyzer.fallback_semantics("screenshot_path_missing")


def test_extract_visual_signature_passes_resolved_local_screenshot(monkeypatch, tmp_path):
    extract_module = importlib.import_module("src.visual_signature.extract_visual_signature")
    screenshot = tmp_path / "screen.png"
    screenshot.write_bytes(b"brand3 image bytes")
    expected = multimodal_analyzer.fallback_semantics(None)
    expected["status"] = "detected"
    expected["fallback_used"] = False

    def fake_analyze_visual_semantics(*, screenshot_path, brand_name):
        assert screenshot_path == str(screenshot)
        assert brand_name == "Example Brand"
        return expected

    monkeypatch.setattr(extract_module, "analyze_visual_semantics", fake_analyze_visual_semantics)

    result = extract_module.extract_visual_signature(
        brand_name="Example Brand",
        website_url="https://example.com",
        screenshot_payload={"path": str(screenshot)},
        adapter=FixtureAdapter(),
    )

    assert result["semantics"] == expected


def test_extract_visual_signature_prefers_section_atlas_for_semantics(monkeypatch, tmp_path):
    extract_module = importlib.import_module("src.visual_signature.extract_visual_signature")
    viewport = tmp_path / "screen.png"
    atlas = tmp_path / "screen.analysis-atlas.png"
    viewport.write_bytes(b"viewport")
    atlas.write_bytes(b"atlas")
    captured: dict[str, object] = {}

    def fake_analyze_visual_semantics(*, screenshot_path, brand_name):
        captured["screenshot_path"] = screenshot_path
        captured["brand_name"] = brand_name
        return multimodal_analyzer.fallback_semantics(None)

    monkeypatch.setattr(extract_module, "analyze_visual_semantics", fake_analyze_visual_semantics)

    extract_module.extract_visual_signature(
        brand_name="Example Brand",
        website_url="https://example.com",
        screenshot_payload={
            "path": str(viewport),
            "analysis_atlas_path": str(atlas),
        },
        adapter=FixtureAdapter(),
    )

    assert captured == {
        "screenshot_path": str(atlas),
        "brand_name": "Example Brand",
    }


def test_atlas_semantics_reports_labeled_section_scope(tmp_path, monkeypatch):
    atlas = tmp_path / "fixture.analysis-atlas.png"
    atlas.write_bytes(b"atlas bytes")
    monkeypatch.setattr(multimodal_analyzer, "BRAND3_LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        multimodal_analyzer,
        "_run_llm_http_call",
        lambda **_kwargs: (
            "ok",
            json.dumps(
                {
                    "aesthetic_style": "editorial",
                    "visual_mood": "confident",
                    "visual_polish_score": 8,
                    "visual_polish_rationale": "Consistent sections.",
                }
            ),
        ),
    )

    result = multimodal_analyzer.analyze_visual_semantics(
        screenshot_path=str(atlas),
        brand_name="Example Brand",
    )

    assert result["status"] == "detected"
    assert result["audit"]["analysis_scope"] == "labeled_section_atlas"
    assert result["audit"]["input_kind"] == "labeled_atlas"
