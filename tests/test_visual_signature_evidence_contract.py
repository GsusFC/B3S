from __future__ import annotations

from pathlib import Path

from src.visual_signature import build_visual_signature_evidence_v1


def _payload() -> dict:
    return {
        "brand_name": "Example",
        "website_url": "https://example.com",
        "analyzed_url": "https://example.com/home",
        "interpretation_status": "interpretable",
        "acquisition": {"adapter": "existing_web_data", "acquired_at": "2026-06-26T10:00:00Z", "warnings": [], "errors": []},
        "logo": {
            "logo_detected": True,
            "favicon_detected": True,
            "textual_brand_mark_detected": False,
            "primary_location": "nav",
            "confidence": 0.82,
            "candidates": [
                {
                    "location": "metadata",
                    "source": "metadata",
                    "confidence": 0.4,
                    "url": "https://example.com/favicon.ico",
                },
                {
                    "location": "nav",
                    "source": "images",
                    "confidence": 0.86,
                    "url": "https://example.com/logo.svg",
                    "alt": "Example logo",
                },
            ],
        },
        "colors": {
            "dominant_colors": ["#ffffff", "#111111"],
            "accent_candidates": ["#2255ff"],
            "palette_complexity": "medium",
        },
        "typography": {"heading_scale": "expressive", "heading_font": "Inter Display", "body_font": "Inter"},
        "layout": {"has_navigation": True, "has_hero": True, "visual_density": "balanced", "layout_patterns": ["grid"]},
        "components": {"primary_ctas": ["Get started"], "components": [{"type": "cta", "count": 1}]},
        "consistency": {"overall_consistency": 0.74},
        "extraction_confidence": {"score": 0.71, "level": "medium", "limitations": []},
        "vision": {
            "screenshot": {
                "available": True,
                "path": "",
                "capture_type": "viewport",
                "page_url": "https://example.com/home",
                "quality": "usable",
                "width": 1440,
                "height": 900,
                "captured_at": "2026-06-26T10:00:01Z",
            },
            "viewport_obstruction": {
                "present": False,
                "type": "none",
                "severity": "none",
                "first_impression_valid": True,
            },
        },
        "semantics": {
            "status": "detected",
            "data": {
                "visual_polish_score": 8,
                "visual_coherence": "Visual system aligns with the product promise.",
                "copy_visual_alignment": "Visible copy and interface cues are aligned.",
                "first_impression_summary": "Polished, product-first and trustworthy.",
                "logo_prominence": "clear",
                "hierarchy_clarity": "clear",
                "cta_salience": "clear",
                "trust_signal_presence": "partial",
            },
        },
    }


def test_visual_signature_evidence_v1_builds_stable_contract_for_usable_capture():
    evidence = build_visual_signature_evidence_v1(_payload())

    assert evidence["schema_version"] == "visual-signature-evidence-v1"
    assert set(evidence) == {
        "schema_version",
        "fingerprint",
        "capture",
        "identity",
        "visual_system",
        "first_impression",
        "copy_visual_alignment",
        "semantics_audit",
        "evidence_health",
        "tile_signals",
        "limitations",
    }
    assert evidence["capture"]["status"] == "usable"
    assert evidence["capture"]["first_fold_evaluable"] is True
    assert evidence["capture"]["content_trust"] == "trusted"
    assert len(evidence["fingerprint"]["normalized_payload_sha256"]) == 64
    assert evidence["identity"]["candidates"][0]["role"] == "real_logo"
    assert evidence["visual_system"]["synthesis"]["visual_tone"] in {"functional", "expressive", "editorial", "unknown"}
    assert evidence["visual_system"]["synthesis"]["distinctiveness"] in {"high", "medium", "low"}
    assert evidence["evidence_health"]["overall"] in {"strong", "limited", "unreliable"}
    assert evidence["evidence_health"]["identity_status"] in {"strong", "partial", "weak"}
    assert evidence["first_impression"]["summary"] == "Polished, product-first and trustworthy."
    assert evidence["copy_visual_alignment"]["summary"] == "Visible copy and interface cues are aligned."
    assert evidence["semantics_audit"]["status"] == "detected"
    assert evidence["evidence_health"]["logo_prominence_status"] == "clear"
    assert evidence["tile_signals"]
    assert {signal["tile"] for signal in evidence["tile_signals"]} >= {"coherencia.C6", "magnetism.MG1", "brand_idea.I1"}
    assert any(signal["effect"] == "supports" for signal in evidence["tile_signals"])
    assert all(signal["source"] in {"heuristic", "llm_multimodal"} for signal in evidence["tile_signals"])
    assert all(signal["evidence_refs"] for signal in evidence["tile_signals"])
    assert all("visual_signal_score:" in signal["rationale"] or signal["rationale"] == "capture_unreliable:blocked" for signal in evidence["tile_signals"])


def test_visual_signature_evidence_gate_blocks_positive_signals_for_unreliable_capture():
    payload = _payload()
    payload["vision"]["viewport_obstruction"] = {
        "present": True,
        "type": "cookie_modal",
        "severity": "blocking",
        "coverage_ratio": 0.92,
        "first_impression_valid": False,
        "confidence": 1.0,
        "signals": ["cookie", "privacy"],
    }

    evidence = build_visual_signature_evidence_v1(payload)

    assert evidence["capture"]["status"] == "blocked"
    assert evidence["capture"]["first_fold_evaluable"] is False
    assert evidence["evidence_health"]["overall"] == "unreliable"
    assert "capture_unreliable:blocked" in evidence["limitations"]
    assert "first_fold_not_evaluable" in evidence["limitations"]
    assert all(signal["effect"] == "insufficient_evidence" for signal in evidence["tile_signals"])
    assert all(signal["rationale"] == "capture_unreliable:blocked" for signal in evidence["tile_signals"])


def test_visual_signature_evidence_gate_blocks_positive_signals_for_blank_capture():
    payload = _payload()
    payload["vision"]["screenshot"]["quality"] = "blank"

    evidence = build_visual_signature_evidence_v1(payload)

    assert evidence["capture"]["status"] == "blocked"
    assert evidence["capture"]["first_fold_evaluable"] is False
    assert all(signal["effect"] == "insufficient_evidence" for signal in evidence["tile_signals"])


def test_visual_signature_evidence_marks_low_detail_capture_as_limited():
    payload = _payload()
    payload["vision"]["screenshot"]["quality"] = "low_detail"

    evidence = build_visual_signature_evidence_v1(payload)

    assert evidence["capture"]["status"] == "limited"
    assert evidence["capture"]["first_fold_evaluable"] is False
    assert "capture_unreliable:limited" in evidence["limitations"]
    assert all(signal["effect"] == "insufficient_evidence" for signal in evidence["tile_signals"])


def test_visual_signature_evidence_treats_multimodal_fallback_as_missing_evidence():
    payload = _payload()
    payload["semantics"] = {
        "status": "unavailable",
        "fallback_used": True,
        "error_type": "api_key_missing",
        "data": {
            "visual_polish_score": None,
            "visual_coherence": "not_detected",
            "copy_visual_alignment": "not_detected",
            "first_impression_summary": "not_detected",
            "logo_prominence": "not_detected",
            "hierarchy_clarity": "not_detected",
            "cta_salience": "not_detected",
            "trust_signal_presence": "not_detected",
        },
    }

    evidence = build_visual_signature_evidence_v1(payload)
    by_tile = {signal["tile"]: signal for signal in evidence["tile_signals"]}

    assert evidence["first_impression"]["summary"] == ""
    assert evidence["copy_visual_alignment"]["summary"] == ""
    assert evidence["evidence_health"]["semantic_alignment_status"] == "unknown"
    assert "multimodal_semantics_unavailable" in evidence["evidence_health"]["warnings"]
    assert by_tile["coherencia.C6"]["effect"] == "insufficient_evidence"
    assert by_tile["coherencia.C6"]["rationale"].startswith("multimodal_semantics_unavailable")
    assert by_tile["brand_idea.I3"]["effect"] == "insufficient_evidence"
    assert by_tile["brand_idea.I3"]["rationale"].startswith("multimodal_semantics_unavailable")
    assert by_tile["core_purpose.PR8"]["effect"] == "insufficient_evidence"
    assert by_tile["core_purpose.PR8"]["rationale"].startswith("multimodal_semantics_unavailable")
    assert by_tile["magnetism.MG1"]["effect"] == "insufficient_evidence"
    assert by_tile["magnetism.MG1"]["rationale"].startswith("multimodal_semantics_unavailable")
    assert by_tile["magnetism.MG7"]["effect"] == "insufficient_evidence"
    assert by_tile["magnetism.MG7"]["rationale"].startswith("multimodal_semantics_unavailable")


def test_visual_signature_evidence_c6_is_insufficient_when_copy_visual_alignment_is_missing():
    payload = _payload()
    payload["semantics"] = {
        "status": "detected",
        "data": {
            "visual_polish_score": 8,
            "visual_coherence": "Visual system aligns with the product promise.",
            "copy_visual_alignment": "",
            "first_impression_summary": "Polished, product-first and trustworthy.",
            "logo_prominence": "clear",
            "hierarchy_clarity": "clear",
            "cta_salience": "clear",
            "trust_signal_presence": "partial",
        },
    }

    evidence = build_visual_signature_evidence_v1(payload)
    by_tile = {signal["tile"]: signal for signal in evidence["tile_signals"]}

    assert by_tile["coherencia.C6"]["effect"] == "insufficient_evidence"
    assert by_tile["coherencia.C6"]["rationale"].startswith("copy_visual_alignment_missing")


def test_visual_signature_evidence_hashes_screenshot_file_when_available(tmp_path: Path):
    screenshot = tmp_path / "shot.png"
    screenshot.write_bytes(b"brand3-shot")
    payload = _payload()
    payload["vision"]["screenshot"]["path"] = str(screenshot)

    first = build_visual_signature_evidence_v1(payload)
    second = build_visual_signature_evidence_v1(payload)

    assert first["fingerprint"]["screenshot_sha256"] == second["fingerprint"]["screenshot_sha256"]
    assert len(first["fingerprint"]["screenshot_sha256"]) == 64
    assert first["fingerprint"]["normalized_payload_sha256"] == second["fingerprint"]["normalized_payload_sha256"]


def test_visual_signature_evidence_persists_partial_capture_contract():
    payload = _payload()
    complete = build_visual_signature_evidence_v1(payload)
    payload["vision"]["screenshot"].update(
        {
            "capture_recovery": "raw_viewport_checkpoint",
            "section_capture_status": "timeout",
            "structured_capture_errors": ["section_analysis_timeout"],
        }
    )

    evidence = build_visual_signature_evidence_v1(payload)

    assert evidence["capture"]["capture_recovery"] == "raw_viewport_checkpoint"
    assert evidence["capture"]["section_capture_status"] == "timeout"
    assert evidence["capture"]["structured_capture_errors"] == ["section_analysis_timeout"]
    assert (
        evidence["fingerprint"]["normalized_payload_sha256"]
        != complete["fingerprint"]["normalized_payload_sha256"]
    )


def test_visual_signature_evidence_normalized_payload_hash_ignores_capture_timestamp_and_path(tmp_path: Path):
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    first_payload = _payload()
    second_payload = _payload()
    first_payload["vision"]["screenshot"]["path"] = str(first_path)
    first_payload["vision"]["screenshot"]["captured_at"] = "2026-06-26T10:00:01Z"
    second_payload["vision"]["screenshot"]["path"] = str(second_path)
    second_payload["vision"]["screenshot"]["captured_at"] = "2026-06-26T10:05:01Z"

    first = build_visual_signature_evidence_v1(first_payload)
    second = build_visual_signature_evidence_v1(second_payload)

    assert first["fingerprint"]["screenshot_sha256"] != second["fingerprint"]["screenshot_sha256"]
    assert first["fingerprint"]["captured_at"] != second["fingerprint"]["captured_at"]
    assert first["fingerprint"]["normalized_payload_sha256"] == second["fingerprint"]["normalized_payload_sha256"]


def _interstitial_audit_data() -> dict:
    """Gemini's real output for the mentelem.com security-check capture."""

    return {
        "visual_polish_score": 7,
        "visual_coherence": "A clean, minimal loading interface.",
        "copy_visual_alignment": "Copy and visuals both describe a verification step.",
        "first_impression_summary": "A clean, functional loading screen indicating a security check is underway.",
        "observed_risks": [
            "Generic visual identity (robot icon)",
            "Lack of actual brand content",
            "Potential for user impatience due to loading screen",
        ],
        "notable_absences": [
            "Brand-specific imagery/typography",
            "Navigation elements",
            "Product/service information",
            "Unique brand personality",
        ],
        "logo_prominence": "weak",
        "hierarchy_clarity": "clear",
        "cta_salience": "not_detected",
        "trust_signal_presence": "not_detected",
    }


_INTERSTITIAL_REASON = "loading_screen+security_check+lack_of_actual_brand_content"


def test_visual_signature_evidence_treats_missing_polish_score_as_unknown_not_zero():
    payload = _payload()
    payload["semantics"]["data"]["visual_polish_score"] = None

    evidence = build_visual_signature_evidence_v1(payload)
    by_tile = {signal["tile"]: signal for signal in evidence["tile_signals"]}

    assert evidence["first_impression"]["visual_polish"] is None
    for tile in ("brand_idea.I3", "magnetism.MG1", "magnetism.MG7"):
        assert by_tile[tile]["effect"] == "insufficient_evidence"
        assert by_tile[tile]["confidence"] == "medium"
        assert by_tile[tile]["rationale"].startswith("score_unavailable:visual_polish")
        assert "polish:unavailable" in by_tile[tile]["rationale"]
    assert all(signal["effect"] != "weakens" for signal in evidence["tile_signals"])


def test_visual_signature_evidence_marks_interstitial_capture_content_untrusted():
    payload = _payload()
    payload["consistency"]["overall_consistency"] = 0.198
    payload["semantics"]["data"] = _interstitial_audit_data()

    evidence = build_visual_signature_evidence_v1(payload)
    capture = evidence["capture"]
    by_tile = {signal["tile"]: signal for signal in evidence["tile_signals"]}

    assert capture["status"] == "usable"
    assert capture["content_trust"] == "untrusted"
    assert capture["content_trust_reason"] == _INTERSTITIAL_REASON
    assert f"capture_content_untrusted:{_INTERSTITIAL_REASON}" in evidence["limitations"]
    assert f"capture_content_untrusted:{_INTERSTITIAL_REASON}" in evidence["evidence_health"]["warnings"]
    # The packet stays evidence-only: the raw negative reading is preserved here
    # and suppressed downstream by the SV9 consumer, not by this producer.
    assert by_tile["coherencia.C6"]["effect"] == "weakens"
    assert by_tile["coherencia.C6"]["confidence"] == "high"


def test_visual_signature_evidence_trusts_detected_audit_without_interstitial_markers():
    payload = _payload()
    payload["semantics"]["data"]["observed_risks"] = ["Limited colour contrast in the hero"]
    payload["semantics"]["data"]["notable_absences"] = []

    evidence = build_visual_signature_evidence_v1(payload)

    assert evidence["capture"]["content_trust"] == "trusted"
    assert evidence["capture"]["content_trust_reason"] is None
    assert not any(item.startswith("capture_content_untrusted") for item in evidence["limitations"])


def test_visual_signature_evidence_content_trust_requires_two_markers_including_an_interstitial_one():
    interstitial_only = _payload()
    interstitial_only["semantics"]["data"]["first_impression_summary"] = "A captcha widget sits above the fold."

    absences_only = _payload()
    absences_only["semantics"]["data"]["observed_risks"] = [
        "Lack of brand content in the hero",
        "No brand content below the fold",
    ]

    assert build_visual_signature_evidence_v1(interstitial_only)["capture"]["content_trust"] == "trusted"
    assert build_visual_signature_evidence_v1(absences_only)["capture"]["content_trust"] == "trusted"


def test_visual_signature_evidence_content_trust_is_unknown_without_multimodal_audit():
    payload = _payload()
    payload["semantics"] = {
        "status": "unavailable",
        "fallback_used": True,
        "error_type": "api_key_missing",
        "data": _interstitial_audit_data(),
    }

    evidence = build_visual_signature_evidence_v1(payload)

    assert evidence["capture"]["content_trust"] == "unknown"
    assert evidence["capture"]["content_trust_reason"] is None
    assert not any(item.startswith("capture_content_untrusted") for item in evidence["limitations"])
