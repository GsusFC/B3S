from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "visual_signature_capture_screenshots.py"
VISION_INPUT_PATH = Path(__file__).resolve().parents[1] / "examples" / "visual_signature" / "vision_calibration_brands.json"


def _load_capturer():
    spec = importlib.util.spec_from_file_location("visual_signature_capture_screenshots", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_load_capture_brands_reads_vision_example_set():
    capturer = _load_capturer()

    brands = capturer.load_capture_brands(VISION_INPUT_PATH)

    assert len(brands) == 5
    assert brands[0].screenshot_path.endswith(".png")
    assert brands[0].capture_type == "viewport"


def test_capture_screenshots_records_success_and_failure(tmp_path):
    capturer = _load_capturer()
    output_dir = tmp_path / "shots"
    manifest_path = tmp_path / "manifest.json"

    brands = [
        capturer.CaptureBrand("Good", "https://good.example", str(output_dir / "good.png")),
        capturer.CaptureBrand("Bad", "https://bad.example", str(output_dir / "bad.png")),
    ]

    def capture_fn(brand_name: str, website_url: str, screenshot_path: str, capture_type: str):
        if brand_name == "Bad":
            raise RuntimeError("capture failed")
        Path(screenshot_path).write_bytes(b"PNG")
        return {
            "source": "playwright",
            "capture_type": capture_type,
            "width": 1440,
            "height": 900 if capture_type == "viewport" else 1200,
            "viewport_width": 1440,
            "viewport_height": 900 if capture_type == "viewport" else 1200,
            "page_url": website_url,
        }

    manifest = capturer.capture_screenshots(
        brands,
        output_dir=output_dir,
        manifest_path=manifest_path,
        capture_fn=capture_fn,
    )

    assert manifest["total"] == 2
    assert manifest["ok"] == 1
    assert manifest["error"] == 1
    assert (output_dir / "good.png").exists()
    assert not (output_dir / "bad.png").exists()
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert saved["results"][0]["screenshot_path"].endswith("good.png")
    assert saved["results"][0]["width"] == 1440
    assert saved["results"][0]["capture_type"] == "viewport"
    assert saved["results"][0]["viewport_height"] == 900
    assert "perceptual_state" not in saved["results"][0]
    assert not (output_dir / "dismissal_audit.json").exists()


def test_capture_screenshots_can_capture_both_variants(tmp_path):
    capturer = _load_capturer()
    output_dir = tmp_path / "shots"
    manifest_path = tmp_path / "manifest.json"

    brands = [
        capturer.CaptureBrand("Good", "https://good.example", str(output_dir / "good.png")),
    ]

    def capture_fn(brand_name: str, website_url: str, screenshot_path: str, capture_type: str):
        Path(screenshot_path).write_bytes(b"PNG")
        return {
            "source": "playwright",
            "capture_type": capture_type,
            "width": 1440,
            "height": 900 if capture_type == "viewport" else 1200,
            "viewport_width": 1440,
            "viewport_height": 900 if capture_type == "viewport" else 1200,
            "page_url": website_url,
        }

    manifest = capturer.capture_screenshots(
        brands,
        output_dir=output_dir,
        manifest_path=manifest_path,
        capture_fn=capture_fn,
        capture_both=True,
    )

    result = manifest["results"][0]
    assert result["capture_type"] == "viewport"
    assert result["secondary_capture_type"] == "full_page"
    assert result["secondary_screenshot_path"].endswith("good.full-page.png")
    assert (output_dir / "good.png").exists()
    assert (output_dir / "good.full-page.png").exists()


def test_capture_screenshots_defaults_to_viewport(tmp_path):
    capturer = _load_capturer()
    output_dir = tmp_path / "shots"
    manifest_path = tmp_path / "manifest.json"

    brands = [
        capturer.CaptureBrand("Good", "https://good.example", str(output_dir / "good.png")),
    ]

    def capture_fn(brand_name: str, website_url: str, screenshot_path: str, capture_type: str):
        Path(screenshot_path).write_bytes(b"PNG")
        assert capture_type == "viewport"
        return {
            "source": "playwright",
            "capture_type": capture_type,
            "width": 1440,
            "height": 900,
            "viewport_width": 1440,
            "viewport_height": 900,
            "page_url": website_url,
        }

    manifest = capturer.capture_screenshots(
        brands,
        output_dir=output_dir,
        manifest_path=manifest_path,
        capture_fn=capture_fn,
    )

    assert manifest["ok"] == 1
    assert manifest["results"][0]["capture_type"] == "viewport"
    assert "perceptual_state" not in manifest["results"][0]
    assert "perceptual_transitions" not in manifest["results"][0]
    assert "mutation_audit" not in manifest["results"][0]


def test_capture_screenshots_with_dismissal_experiment_writes_audit(tmp_path):
    capturer = _load_capturer()
    output_dir = tmp_path / "shots"
    manifest_path = tmp_path / "manifest.json"

    brands = [
        capturer.CaptureBrand("Success", "https://success.example", str(output_dir / "success.png")),
        capturer.CaptureBrand("Failure", "https://failure.example", str(output_dir / "failure.png")),
    ]

    def capture_fn(
        brand_name: str,
        website_url: str,
        screenshot_path: str,
        capture_type: str,
        *,
        attempt_dismiss_obstructions: bool = False,
    ):
        path = Path(screenshot_path)
        path.write_bytes(b"PNG")
        result = {
            "source": "playwright",
            "capture_type": capture_type,
            "capture_variant": "raw_viewport",
            "raw_screenshot_path": screenshot_path,
            "width": 1440,
            "height": 900,
            "viewport_width": 1440,
            "viewport_height": 900,
            "page_url": website_url,
            "before_obstruction": {
                "present": True,
                "type": "cookie_banner",
                "severity": "major",
                "coverage_ratio": 0.42,
                "first_impression_valid": False,
                "confidence": 0.91,
                "signals": ["cookie banner"],
                "limitations": [],
            },
            "candidate_click_targets": [
                {
                    "label": "Accept all",
                    "normalized_label": "accept all",
                    "method": "accept_all",
                    "selector": capturer.DISMISSAL_TARGET_SELECTOR,
                    "reason": None,
                    "affordance_category": "consent_accept",
                    "interaction_policy": "safe_to_dismiss",
                    "affordance_confidence": 0.94,
                    "affordance_evidence": {
                        "visible_text": ["Accept all cookies"],
                        "aria_labels": [],
                        "titles": [],
                        "roles": ["button"],
                        "svg_icon_semantics": [],
                        "dom_context": ["cookie", "consent"],
                        "overlay_context": ["cookie_banner"],
                    },
                },
            ],
            "rejected_click_targets": [
                {
                    "label": "Close this popup",
                    "normalized_label": "close this popup",
                    "method": None,
                    "selector": capturer.DISMISSAL_TARGET_SELECTOR,
                    "reason": "not_exact_match",
                    "affordance_category": "close_control",
                    "interaction_policy": "safe_to_dismiss",
                    "affordance_confidence": 0.92,
                    "affordance_evidence": {
                        "visible_text": ["Close this popup"],
                        "aria_labels": [],
                        "titles": [],
                        "roles": ["button"],
                        "svg_icon_semantics": [],
                        "dom_context": ["cookie", "consent"],
                        "overlay_context": ["cookie_banner"],
                    },
                },
                {
                    "label": "OK",
                    "normalized_label": "ok",
                    "method": None,
                    "selector": capturer.DISMISSAL_TARGET_SELECTOR,
                    "reason": "not_exact_match",
                    "affordance_category": "ambiguous_action",
                    "interaction_policy": "requires_human_review",
                    "affordance_confidence": 0.44,
                    "affordance_evidence": {
                        "visible_text": ["OK"],
                        "aria_labels": [],
                        "titles": [],
                        "roles": ["button"],
                        "svg_icon_semantics": [],
                        "dom_context": [],
                        "overlay_context": ["newsletter_modal"],
                    },
                },
            ],
            "raw_viewport_metrics": {
                "viewport_whitespace_ratio": 0.18,
                "viewport_visual_density": 0.82,
                "viewport_composition": "dense",
                "palette_color_count": 9,
            },
            "dismissal_attempted": attempt_dismiss_obstructions,
            "dismissal_successful": brand_name == "Success",
            "dismissal_method": "accept_all",
            "clicked_text": "Accept all",
            "evidence_integrity_notes": ["raw_viewport_preserved_as_primary_evidence"],
            "perceptual_state_data": {
                "current_state": "MINIMALLY_MUTATED_STATE" if brand_name == "Success" else "REVIEW_REQUIRED_STATE",
                "transitions": [
                    {"reason": "raw_capture_created"},
                    {"reason": "viewport_obstruction_detected"},
                    {"reason": "exact_safe_affordance_detected"},
                    {"reason": "safe_mutation_attempted"},
                    {"reason": "safe_mutation_succeeded" if brand_name == "Success" else "safe_mutation_failed"},
                ],
                "mutation_results": [
                    {
                        "mutation_audit": {
                            "mutation_id": f"{brand_name.lower()}-mutation",
                            "mutation_type": "cookie_dismissal",
                            "before_state": "ELIGIBLE_FOR_SAFE_INTERVENTION",
                            "after_state": "MINIMALLY_MUTATED_STATE" if brand_name == "Success" else "REVIEW_REQUIRED_STATE",
                            "attempted": True,
                            "successful": brand_name == "Success",
                            "reversible": True,
                            "risk_level": "low",
                            "trigger": "safe_mutation_attempted",
                            "evidence_preserved": True,
                            "before_artifact_ref": str(path),
                            "after_artifact_ref": str(path.with_name(f"{path.stem}.clean-attempt{path.suffix}")),
                            "integrity_notes": ["raw_state_preserved_as_primary_evidence"],
                        }
                    }
                ],
            },
        }
        if attempt_dismiss_obstructions:
            clean_path = path.with_name(f"{path.stem}.clean-attempt{path.suffix}")
            clean_path.write_bytes(b"PNG")
            result.update(
                {
                    "clean_attempt_screenshot_path": str(clean_path),
                    "clean_attempt_capture_variant": "clean_attempt",
                    "after_obstruction": {
                        "present": brand_name != "Failure",
                        "type": "cookie_banner",
                        "severity": "minor" if brand_name == "Success" else "major",
                        "coverage_ratio": 0.12 if brand_name == "Success" else 0.42,
                        "first_impression_valid": True if brand_name == "Success" else False,
                        "confidence": 0.73,
                        "signals": ["cookie banner dismissed"] if brand_name == "Success" else ["cookie banner"],
                        "limitations": [],
                    },
                    "clean_attempt_metrics": {
                        "viewport_whitespace_ratio": 0.31 if brand_name == "Success" else 0.18,
                        "viewport_visual_density": 0.69 if brand_name == "Success" else 0.82,
                        "viewport_composition": "balanced" if brand_name == "Success" else "dense",
                        "palette_color_count": 7 if brand_name == "Success" else 9,
                    },
                }
            )
            if brand_name == "Success":
                result["evidence_integrity_notes"].append("clean_attempt_is_supplemental_only; raw_viewport_remains_primary")
            else:
                result["evidence_integrity_notes"].append("clean_attempt_did_not_materially_reduce_obstruction")
        return result

    manifest = capturer.capture_screenshots(
        brands,
        output_dir=output_dir,
        manifest_path=manifest_path,
        capture_fn=capture_fn,
        attempt_dismiss_obstructions=True,
    )

    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["attempt_dismiss_obstructions"] is True
    assert manifest["results"][0]["raw_screenshot_path"].endswith("success.png")
    assert manifest["results"][0]["clean_attempt_capture_variant"] == "clean_attempt"
    assert manifest["results"][0]["clean_attempt_screenshot_path"].endswith("success.clean-attempt.png")
    assert manifest["results"][0]["dismissal_attempted"] is True
    assert manifest["results"][0]["dismissal_successful"] is True
    assert manifest["results"][1]["dismissal_attempted"] is True
    assert manifest["results"][1]["dismissal_successful"] is False
    assert manifest["results"][0]["perceptual_state"] == "MINIMALLY_MUTATED_STATE"
    assert manifest["results"][1]["perceptual_state"] == "REVIEW_REQUIRED_STATE"
    assert [transition["reason"] for transition in manifest["results"][0]["perceptual_transitions"]] == [
        "raw_capture_created",
        "viewport_obstruction_detected",
        "exact_safe_affordance_detected",
        "safe_mutation_attempted",
        "safe_mutation_succeeded",
    ]
    assert [transition["reason"] for transition in manifest["results"][1]["perceptual_transitions"]] == [
        "raw_capture_created",
        "viewport_obstruction_detected",
        "exact_safe_affordance_detected",
        "safe_mutation_attempted",
        "safe_mutation_failed",
    ]
    assert manifest["results"][0]["mutation_audit"]["successful"] is True
    assert saved["dismissal_audit"].endswith("dismissal_audit.json")
    saved_rows = saved["results"]
    assert saved_rows[0]["perceptual_state"] == "MINIMALLY_MUTATED_STATE"
    assert saved_rows[1]["perceptual_state"] == "REVIEW_REQUIRED_STATE"
    assert saved_rows[0]["mutation_audit"]["successful"] is True
    assert saved_rows[1]["mutation_audit"]["successful"] is False
    audit = json.loads((output_dir / "dismissal_audit.json").read_text(encoding="utf-8"))
    markdown = (output_dir / "dismissal_audit.md").read_text(encoding="utf-8")
    assert audit["dismissal_success_rate"] == 0.5
    assert len(audit["failed_dismissals"]) == 1
    assert len(audit["materially_changed_cases"]) == 1
    assert audit["state_distribution"]["MINIMALLY_MUTATED_STATE"] == 1
    assert audit["state_distribution"]["REVIEW_REQUIRED_STATE"] == 1
    assert audit["transition_reason_distribution"]["raw_capture_created"] == 2
    assert audit["mutation_summary"]["successful"] == 1
    assert audit["clean_attempt_quality_distribution"]["clear_improvement"] == 1
    assert audit["clean_attempt_quality_distribution"]["no_material_improvement"] == 1
    assert audit["results"][0]["clean_attempt_quality"] == "clear_improvement"
    assert audit["results"][1]["clean_attempt_quality"] == "no_material_improvement"
    assert audit["materially_changed_cases"][0]["clean_attempt_quality"] == "clear_improvement"
    assert audit["affordance_category_distribution"]["consent_accept"] >= 1
    assert audit["interaction_policy_distribution"]["safe_to_dismiss"] >= 1
    assert audit["requires_human_review_candidates_encountered"] >= 1
    assert "Affordance owners" in markdown
    assert "Dismissal success rate" in markdown
    assert "Clean attempt quality" in markdown
    assert "Affordance categories" in markdown
    assert "Success" in markdown
    assert (output_dir / "success.png").exists()
    assert (output_dir / "success.clean-attempt.png").exists()
    assert (output_dir / "failure.png").exists()
    assert (output_dir / "failure.clean-attempt.png").exists()


def test_capture_screenshots_persists_state_machine_fields_in_manifest_json(tmp_path):
    capturer = _load_capturer()
    output_dir = tmp_path / "shots"
    manifest_path = tmp_path / "manifest.json"

    brands = [
        capturer.CaptureBrand("Stateful", "https://stateful.example", str(output_dir / "stateful.png")),
    ]

    def capture_fn(
        brand_name: str,
        website_url: str,
        screenshot_path: str,
        capture_type: str,
        *,
        attempt_dismiss_obstructions: bool = False,
    ):
        Path(screenshot_path).write_bytes(b"PNG")
        return {
            "source": "playwright",
            "capture_type": capture_type,
            "capture_variant": "raw_viewport",
            "raw_screenshot_path": screenshot_path,
            "width": 1440,
            "height": 900,
            "viewport_width": 1440,
            "viewport_height": 900,
            "page_url": website_url,
            "before_obstruction": {
                "present": False,
                "type": "none",
                "severity": "none",
                "coverage_ratio": 0.0,
                "first_impression_valid": True,
                "confidence": 0.0,
                "signals": [],
                "limitations": [],
            },
            "raw_viewport_metrics": {
                "viewport_whitespace_ratio": 0.5,
                "viewport_visual_density": 0.5,
                "viewport_composition": "balanced_blocks",
                "palette_color_count": 3,
            },
            "dismissal_attempted": False,
            "dismissal_successful": False,
            "perceptual_state": "RAW_STATE",
            "perceptual_transitions": [
                {"reason": "raw_capture_created"},
                {"reason": "no_obstruction_detected"},
            ],
            "mutation_audit": None,
        }

    manifest = capturer.capture_screenshots(
        brands,
        output_dir=output_dir,
        manifest_path=manifest_path,
        capture_fn=capture_fn,
        attempt_dismiss_obstructions=True,
    )

    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["results"][0]["perceptual_state"] == "RAW_STATE"
    assert saved["results"][0]["perceptual_state"] == "RAW_STATE"
    assert [transition["reason"] for transition in saved["results"][0]["perceptual_transitions"]] == [
        "raw_capture_created",
        "no_obstruction_detected",
    ]
    assert saved["results"][0]["mutation_audit"] is None


def test_capture_screenshots_with_dismissal_experiment_default_manifest_omits_state_fields(tmp_path):
    capturer = _load_capturer()
    output_dir = tmp_path / "shots"
    manifest_path = tmp_path / "manifest.json"

    brands = [
        capturer.CaptureBrand("Plain", "https://plain.example", str(output_dir / "plain.png")),
    ]

    def capture_fn(brand_name: str, website_url: str, screenshot_path: str, capture_type: str):
        Path(screenshot_path).write_bytes(b"PNG")
        return {
            "source": "playwright",
            "capture_type": capture_type,
            "capture_variant": "raw_viewport",
            "raw_screenshot_path": screenshot_path,
            "width": 1440,
            "height": 900,
            "viewport_width": 1440,
            "viewport_height": 900,
            "page_url": website_url,
        }

    manifest = capturer.capture_screenshots(
        brands,
        output_dir=output_dir,
        manifest_path=manifest_path,
        capture_fn=capture_fn,
    )

    result = manifest["results"][0]
    assert "perceptual_state" not in result
    assert "perceptual_transitions" not in result
    assert "mutation_audit" not in result
    assert not (output_dir / "dismissal_audit.json").exists()


def test_perceptual_state_machine_no_obstruction_transition():
    capturer = _load_capturer()
    page = _FakePage([])
    raw_snapshot = {
        "obstruction": {
            "present": False,
            "type": "none",
            "severity": "none",
            "coverage_ratio": 0.0,
            "first_impression_valid": True,
            "confidence": 0.0,
            "signals": [],
            "limitations": [],
        }
    }

    context = capturer._prepare_perceptual_state_machine(
        page=page,
        raw_snapshot=raw_snapshot,
        raw_artifact_ref="raw://viewport",
        attempt_dismiss_obstructions=True,
    )

    assert context is not None
    machine = context["machine"]
    payload = machine.to_dict()
    assert payload["current_state"] == "RAW_STATE"
    assert [transition["reason"] for transition in payload["transitions"]] == [
        "raw_capture_created",
        "no_obstruction_detected",
    ]


def test_perceptual_state_machine_cookie_modal_exact_affordance_transition():
    capturer = _load_capturer()
    page = _FakePage(["Accept all", "Manage choices"])
    raw_snapshot = {
        "obstruction": {
            "present": True,
            "type": "cookie_modal",
            "severity": "major",
            "coverage_ratio": 0.42,
            "first_impression_valid": False,
            "confidence": 0.9,
            "signals": [],
            "limitations": [],
        }
    }

    context = capturer._prepare_perceptual_state_machine(
        page=page,
        raw_snapshot=raw_snapshot,
        raw_artifact_ref="raw://viewport",
        attempt_dismiss_obstructions=True,
    )

    assert context is not None
    payload = context["machine"].to_dict()
    assert payload["current_state"] == "ELIGIBLE_FOR_SAFE_INTERVENTION"
    assert any(transition["reason"] == "exact_safe_affordance_detected" for transition in payload["transitions"])


def test_perceptual_state_machine_login_wall_blocked_transition():
    capturer = _load_capturer()
    page = _FakePage(["Sign in", "Close"])
    raw_snapshot = {
        "obstruction": {
            "present": True,
            "type": "login_wall",
            "severity": "blocking",
            "coverage_ratio": 0.92,
            "first_impression_valid": False,
            "confidence": 0.98,
            "signals": [],
            "limitations": [],
        }
    }

    context = capturer._prepare_perceptual_state_machine(
        page=page,
        raw_snapshot=raw_snapshot,
        raw_artifact_ref="raw://viewport",
        attempt_dismiss_obstructions=True,
    )

    assert context is not None
    payload = context["machine"].to_dict()
    assert payload["current_state"] == "UNSAFE_MUTATION_BLOCKED"
    assert any(transition["reason"] == "protected_environment_detected" for transition in payload["transitions"])


def test_failed_dismissal_records_mutation_audit_and_preserves_raw():
    capturer = _load_capturer()
    machine = capturer.PerceptualStateMachine.from_raw_capture(evidence_refs=["raw://viewport"])
    machine.classify_obstruction(
        {
            "present": True,
            "type": "cookie_modal",
            "severity": "major",
            "coverage_ratio": 0.42,
            "first_impression_valid": False,
            "confidence": 0.9,
            "signals": [],
            "limitations": [],
        },
        evidence_refs=["raw://viewport"],
    )
    machine.evaluate_eligibility(
        {
            "present": True,
            "type": "cookie_modal",
            "severity": "major",
            "coverage_ratio": 0.42,
            "first_impression_valid": False,
            "confidence": 0.9,
            "signals": [],
            "limitations": [],
        },
        affordance_labels=["Accept all"],
        evidence_refs=["raw://viewport"],
    )

    mutation = machine.classify_mutation(
        before_state=machine.current_state,
        attempted=True,
        successful=False,
        reversible=True,
        evidence_preserved=True,
        mutation_type="cookie_dismissal",
        trigger="safe_mutation_attempted",
        before_artifact_ref="raw://viewport",
        after_artifact_ref="clean://viewport",
        evidence_refs=["raw://viewport", "clean://viewport"],
        confidence=0.9,
        notes=["raw_viewport_preserved_as_primary_evidence"],
        risk_level="low",
        mutation_id="mutation-1",
    )

    assert mutation.mutation_audit.attempted is True
    assert mutation.mutation_audit.successful is False
    assert mutation.mutation_audit.before_artifact_ref == "raw://viewport"
    assert mutation.mutation_audit.after_artifact_ref == "clean://viewport"
    assert machine.current_state == "REVIEW_REQUIRED_STATE"


class _FakeElement:
    def __init__(self, label: str, *, visible: bool = True, click_error: str | None = None, localization: dict[str, object] | None = None):
        self.label = label
        self.visible = visible
        self.click_error = click_error
        self.localization = localization or {}
        self.click_calls: list[dict[str, object]] = []

    def is_visible(self):
        return self.visible

    def inner_text(self):
        return self.label

    def text_content(self):
        return self.label

    def get_attribute(self, attr: str):
        return {"aria-label": self.label, "title": self.label, "value": self.label}.get(attr)

    def click(self, timeout: int = 2500, **kwargs):
        self.click_calls.append({"timeout": timeout, **kwargs})
        if self.click_error:
            raise RuntimeError(self.click_error)

    def evaluate(self, script: str):
        return self.localization


class _FakeLocator:
    def __init__(self, elements: list[_FakeElement]):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index: int):
        return self.elements[index]


class _FakePage:
    def __init__(self, labels: list[str], *, hidden: set[str] | None = None, localizations: dict[str, dict[str, object]] | None = None):
        hidden = hidden or set()
        localizations = localizations or {}
        self.elements = [
            _FakeElement(label, visible=label not in hidden, localization=localizations.get(label))
            for label in labels
        ]

    def locator(self, selector: str):
        self.selector = selector
        return _FakeLocator(self.elements)


class _FakeSnapshotPage:
    def __init__(self, rows=None, *, error: Exception | None = None):
        self.rows = rows
        self.error = error

    def evaluate(self, script: str):
        if self.error:
            raise self.error
        return self.rows

    def content(self):
        return "<html>fallback cookie modal</html>"


def test_visible_obstruction_dom_snapshot_serializes_visible_overlay_rows():
    capturer = _load_capturer()
    page = _FakeSnapshotPage(
        [
            {
                "tag": "div",
                "id": "cookie-banner",
                "className": "fixed bottom-0",
                "role": "dialog",
                "ariaModal": "true",
                "ariaLabel": "Cookie banner",
                "position": "fixed",
                "zIndex": "1000",
                "width": 320,
                "height": 160,
                "top": 720,
                "left": 32,
                "text": "Accept All Reject All Cookies Settings",
            }
        ]
    )

    snapshot = capturer._visible_obstruction_dom_snapshot(page)

    assert "visible-overlay" in snapshot
    assert "cookie-banner" in snapshot
    assert "Accept All Reject All" in snapshot


def test_visible_obstruction_dom_snapshot_returns_empty_when_playwright_eval_fails():
    capturer = _load_capturer()
    page = _FakeSnapshotPage(error=RuntimeError("js unavailable"))

    assert capturer._visible_obstruction_dom_snapshot(page) == ""


def test_visible_obstruction_dom_snapshot_returns_empty_when_no_visible_overlay_rows():
    capturer = _load_capturer()
    page = _FakeSnapshotPage([])

    assert capturer._visible_obstruction_dom_snapshot(page) == ""


def test_cookie_modal_accept_all_is_safe_candidate():
    capturer = _load_capturer()
    page = _FakePage(["Accept all", "Manage choices"])
    obstruction = {"present": True, "type": "cookie_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["block_reason"] is None
    assert discovery["candidate_click_targets"][0]["method"] == "accept_all"
    assert discovery["candidate_click_targets"][0]["affordance_category"] == "consent_accept"
    assert discovery["candidate_click_targets"][0]["interaction_policy"] == "safe_to_dismiss"
    assert any(item["reason"] == "manage_choices_not_safe" for item in discovery["rejected_click_targets"])
    assert dismissal["attempted"] is True
    assert dismissal["method"] == "accept_all"
    assert dismissal["clicked_text"] == "Accept all"
    assert page.elements[0].click_calls[0] == {"timeout": 4000}


def test_dismissal_discovery_ignores_safe_button_outside_current_viewport():
    capturer = _load_capturer()
    page = _FakePage(
        ["Accept all"],
        localizations={
            "Accept all": {
                "bounding_box": {"x": 64, "y": 1280, "width": 180, "height": 48},
                "viewport_width": 1440,
                "viewport_height": 900,
            }
        },
    )
    obstruction = {"present": True, "type": "cookie_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["candidate_click_targets"] == []
    assert discovery["rejected_click_targets"] == []
    assert discovery["block_reason"] == "no_safe_cookie_button_found"
    assert dismissal["attempted"] is False


def test_dismissal_discovery_keeps_safe_button_inside_current_viewport():
    capturer = _load_capturer()
    page = _FakePage(
        ["Reject All"],
        localizations={
            "Reject All": {
                "bounding_box": {"x": 32, "y": 812, "width": 150, "height": 44},
                "viewport_width": 1440,
                "viewport_height": 900,
            }
        },
    )
    obstruction = {"present": True, "type": "cookie_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["candidate_click_targets"][0]["method"] == "reject_all"


def test_dismissal_discovery_suppresses_unrelated_header_rejections():
    capturer = _load_capturer()
    page = _FakePage(
        ["Pricing", "Manage choices"],
        localizations={
            "Pricing": {
                "bounding_box": {"x": 820, "y": 24, "width": 84, "height": 32},
                "viewport_width": 1440,
                "viewport_height": 900,
                "dom_ancestry": [{"tag": "header", "id": "site-header", "role": "navigation"}],
                "viewport_location": "top_right",
                "position": "sticky",
                "z_index": "100",
                "role_hint": "navigation",
            },
            "Manage choices": {
                "bounding_box": {"x": 64, "y": 812, "width": 180, "height": 44},
                "viewport_width": 1440,
                "viewport_height": 900,
                "dom_ancestry": [{"tag": "div", "id": "cookie-modal", "role": "dialog", "text": "cookie consent"}],
                "viewport_location": "bottom_left",
                "position": "fixed",
                "z_index": "1000",
                "role_hint": "dialog",
            },
        },
    )
    obstruction = {"present": True, "type": "cookie_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)

    rejected_labels = [item["label"] for item in discovery["rejected_click_targets"]]
    assert "Pricing" not in rejected_labels
    assert "Manage choices" in rejected_labels


def test_cookie_modal_i_agree_is_safe_candidate():
    capturer = _load_capturer()
    page = _FakePage(["I agree", "Privacy settings"])
    obstruction = {"present": True, "type": "cookie_banner", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["candidate_click_targets"][0]["method"] == "agree"
    assert discovery["candidate_click_targets"][0]["affordance_category"] == "consent_accept"
    assert discovery["candidate_click_targets"][0]["interaction_policy"] == "safe_to_dismiss"


def test_cookie_modal_spanish_accept_is_safe_candidate():
    capturer = _load_capturer()
    page = _FakePage(["Aceptar todas las cookies", "Configurar preferencias"])
    obstruction = {"present": True, "type": "cookie_banner", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["candidate_click_targets"][0]["method"] == "accept_all"
    assert discovery["candidate_click_targets"][0]["affordance_category"] == "consent_accept"
    assert discovery["candidate_click_targets"][0]["interaction_policy"] == "safe_to_dismiss"
    assert dismissal["attempted"] is True
    assert dismissal["clicked_text"] == "Aceptar todas las cookies"


def test_cookie_modal_spanish_reject_is_safe_candidate():
    capturer = _load_capturer()
    page = _FakePage(["Rechazar todas", "Configurar"])
    obstruction = {"present": True, "type": "cookie_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["candidate_click_targets"][0]["method"] == "reject_all"
    assert discovery["candidate_click_targets"][0]["affordance_category"] == "consent_reject"
    assert discovery["candidate_click_targets"][0]["interaction_policy"] == "safe_to_dismiss"
    assert any(item["reason"] == "manage_choices_not_safe" for item in discovery["rejected_click_targets"])


def test_newsletter_typed_obstruction_with_cookie_signals_uses_consent_context():
    capturer = _load_capturer()
    page = _FakePage(["Aceptar", "Rechazar", "Personalizar"])
    obstruction = {
        "present": True,
        "type": "newsletter_modal",
        "confidence": 0.9,
        "signals": ["dom_keyword:cookie", "dom_overlay_term:dialog"],
        "page_level_signals": ["dom_keyword:cookies"],
    }

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["candidate_click_targets"][0]["label"] == "Aceptar"
    assert discovery["candidate_click_targets"][0]["method"] == "accept"
    assert discovery["candidate_click_targets"][0]["affordance_category"] == "consent_accept"
    assert discovery["candidate_click_targets"][0]["interaction_policy"] == "safe_to_dismiss"
    assert any(item["normalized_label"] == "personalizar" and item["reason"] == "manage_choices_not_safe" for item in discovery["rejected_click_targets"])
    assert dismissal["attempted"] is True


def test_promo_modal_with_only_page_level_cookie_terms_does_not_remap_to_cookie_context():
    capturer = _load_capturer()
    page = _FakePage(["Aceptar", "Rechazar", "Personalizar"])
    obstruction = {
        "present": True,
        "type": "promo_modal",
        "confidence": 0.9,
        "signals": ["dom_keyword:cookie", "dom_bottom_aligned_container_pattern"],
        "page_level_signals": ["dom_keyword:cookies"],
        "overlay_level_signals": [],
    }

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["selected_candidate"] is None
    assert discovery["block_reason"] == "no_safe_close_button_found"
    assert dismissal["attempted"] is False


def test_low_confidence_cookie_obstruction_is_not_eligible_for_dismissal():
    capturer = _load_capturer()
    page = _FakePage(["Accept all"])
    obstruction = {"present": True, "type": "cookie_modal", "confidence": 0.4, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is False
    assert discovery["block_reason"] == "obstruction_confidence_too_low"
    assert dismissal["attempted"] is False


def test_cookie_modal_long_banner_text_is_not_clicked_even_with_accept_phrase():
    capturer = _load_capturer()
    page = _FakePage(
        [
            "Usamos cookies propias y de terceros para analizar el trafico y personalizar contenido. Aceptar todas las cookies",
        ]
    )
    obstruction = {"present": True, "type": "cookie_banner", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["selected_candidate"] is None
    assert discovery["block_reason"] == "no_safe_cookie_button_found"
    assert discovery["rejected_click_targets"][0]["reason"] in {"not_safe_cookie_action", "manage_choices_not_safe"}
    assert dismissal["attempted"] is False


def test_cookie_modal_manage_choices_is_not_clicked():
    capturer = _load_capturer()
    page = _FakePage(["Manage choices"])
    obstruction = {"present": True, "type": "cookie_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["selected_candidate"] is None
    assert discovery["block_reason"] == "no_safe_cookie_button_found"
    assert discovery["rejected_click_targets"][0]["affordance_category"] == "ambiguous_action"
    assert discovery["rejected_click_targets"][0]["interaction_policy"] == "requires_human_review"
    assert dismissal["attempted"] is False
    assert dismissal["dismissal_block_reason"] == "no_safe_cookie_button_found"


def test_cookie_modal_signup_cta_is_subscription_rejection_not_preferences():
    capturer = _load_capturer()
    page = _FakePage(["Sign up"])
    obstruction = {"present": True, "type": "cookie_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)

    assert discovery["selected_candidate"] is None
    assert discovery["rejected_click_targets"][0]["reason"] == "unsafe_subscription_action"


def test_newsletter_modal_x_close_is_safe_candidate():
    capturer = _load_capturer()
    page = _FakePage(["X", "Subscribe"])
    obstruction = {"present": True, "type": "newsletter_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["candidate_click_targets"][0]["method"] == "close"
    assert discovery["candidate_click_targets"][0]["affordance_category"] == "dismiss_control"
    assert discovery["candidate_click_targets"][0]["interaction_policy"] == "safe_to_dismiss"
    assert any(item["reason"] == "newsletter_call_to_action_not_safe" for item in discovery["rejected_click_targets"])
    assert dismissal["attempted"] is True
    assert dismissal["method"] == "close"


def test_newsletter_modal_subscribe_is_not_clicked():
    capturer = _load_capturer()
    page = _FakePage(["Subscribe"])
    obstruction = {"present": True, "type": "newsletter_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["selected_candidate"] is None
    assert discovery["block_reason"] == "no_safe_close_button_found"
    assert discovery["rejected_click_targets"][0]["affordance_category"] == "subscription_action"
    assert discovery["rejected_click_targets"][0]["interaction_policy"] == "unsafe_to_mutate"
    assert dismissal["attempted"] is False


def test_login_wall_is_not_clicked():
    capturer = _load_capturer()
    page = _FakePage(["Sign in", "Close"])
    obstruction = {"present": True, "type": "login_wall", "confidence": 1.0, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is False
    assert discovery["block_reason"] == "obstruction_type_not_eligible:login_wall"
    assert dismissal["attempted"] is False
    assert dismissal["dismissal_block_reason"] == "obstruction_type_not_eligible:login_wall"


def test_ambiguous_ok_button_requires_review_and_is_not_clicked():
    capturer = _load_capturer()
    page = _FakePage(["OK"])
    obstruction = {"present": True, "type": "newsletter_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)
    dismissal = capturer._attempt_obstruction_dismissal(page, obstruction)

    assert discovery["eligible"] is True
    assert discovery["selected_candidate"] is None
    assert discovery["rejected_click_targets"][0]["affordance_category"] == "ambiguous_action"
    assert discovery["rejected_click_targets"][0]["interaction_policy"] == "requires_human_review"
    assert dismissal["attempted"] is False


def test_dismissal_discovery_includes_affordance_owner_diagnostics():
    capturer = _load_capturer()
    page = _FakePage(
        ["Close chat", "Close"],
        localizations={
            "Close chat": {
                "bounding_box": {"x": 1080, "y": 760, "width": 220, "height": 80},
                "dom_ancestry": [
                    {"tag": "div", "id": "chat-widget", "role": "complementary", "text": "chat"},
                    {"tag": "button", "text": "Close chat"},
                ],
                "viewport_location": "bottom_right",
                "position": "fixed",
                "z_index": "999",
                "aria_modal": "",
                "role_hint": "button",
                "proximity_context": ["chat_widget"],
            },
            "Close": {
                "bounding_box": {"x": 720, "y": 180, "width": 360, "height": 320},
                "dom_ancestry": [
                    {"tag": "div", "id": "cookie-modal", "role": "dialog", "aria_modal": "true", "text": "cookie consent"},
                    {"tag": "button", "text": "Close"},
                ],
                "viewport_location": "center",
                "position": "fixed",
                "z_index": "1200",
                "aria_modal": "true",
                "role_hint": "dialog",
                "proximity_context": ["cookie_modal"],
            },
        },
    )
    obstruction = {"present": True, "type": "cookie_modal", "confidence": 0.9, "signals": []}

    discovery = capturer._discover_dismissal_targets(page, obstruction)

    assert discovery["candidate_click_targets"][0]["affordance_owner"] == "active_obstruction"
    assert discovery["candidate_click_targets"][0]["owner_confidence"] >= 0.9
    assert discovery["rejected_click_targets"][0]["affordance_owner"] == "unrelated_chat_widget"
    assert discovery["rejected_click_targets"][0]["owner_confidence"] >= 0.9
