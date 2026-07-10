"""Runtime orchestration for MagnetismExtractor.

This module intentionally keeps the acquisition + result-enrichment flow for
legacy and audit-snapshot extraction paths.
"""

from __future__ import annotations

import os
from typing import Any

from src.config import (
    BRAND3_BRAND_RESEARCH_GRAPH_PACK,
    BRAND3_CONTEXTDEV_VISUAL_ENRICHMENT,
    BRAND3_MAGNETISM_RESEARCH_PACK_TLDR,
)
from src.collectors.web_collector import WebCollector
from src.features.magnetism.analyst_tldr import run_analyst_tldr_pass as _run_analyst_tldr_pass
from src.features.magnetism.content_distiller import ContentDistiller
from src.reports.brand_context_brief import build_brand_context_brief
from src.reports.canonical_evidence import build_canonical_brand_evidence
from src.reports.strategic_evidence_packet import StrategicEvidencePacket
from src.research.contextdev_research_pack_dry_run import build_contextdev_research_pack_dry_run
from src.research.research_pack_facade import RecommendedResearchPack, build_recommended_research_pack
from src.research.research_pack_quality import evaluate_research_pack_quality, evaluate_research_pack_quality_gate
from src.visual_signature.vision.multimodal_analyzer import analyze_visual_semantics

from src.features.magnetism.extractor_constants import CANONICAL_EXTRACTION_MODE


def _resolve_run_analyst_tldr_pass(**kwargs: Any) -> dict[str, Any]:
    """Resolve analyst pass through the public extractor facade to keep monkeypatch points stable."""
    try:
        from src.features.magnetism import extractor as extractor_module

        patched = getattr(extractor_module, "run_analyst_tldr_pass")
        if callable(patched):
            return patched(**kwargs)
    except Exception:
        pass
    return _run_analyst_tldr_pass(**kwargs)


class MagnetismExtractorRuntimeMixin:
    """Orchestrates input acquisition and output enrichment for extraction."""

    def extract(
        self,
        url: str | None,
        manual_text: str | None = None,
        brand_name: str | None = None,
    ) -> dict[str, Any]:
        """Legacy direct acquisition path for ad-hoc/debug Magnetism scans.

        Production Brand3 lenses should use extract_from_audit_snapshot() so Brand
        Audit remains the single canonical acquisition pipeline.
        """
        url_str = (url or "").strip()
        manual_str = (manual_text or "").strip()
        brand_name = brand_name or self._infer_brand_name(url_str)

        web_markdown = ""
        screenshot_path = None
        temp_screenshot_path = None
        web_collector_error = ""
        source_provider = "manual_evidence" if manual_str else "direct_input"
        content_distillation_summary: dict[str, Any] | None = None

        if url_str:
            try:
                collector = WebCollector()
                web_data = collector.scrape(url_str)
                web_markdown = web_data.markdown_content or ""
                screenshot_path = web_data.screenshot_path
                web_collector_error = web_data.error
                source_provider = web_data.content_source or ("web_collector" if web_markdown else "web_collector_empty")

                if not screenshot_path:
                    try:
                        from src.services.brand_service import _take_playwright_screenshot

                        normalized_url = url_str if "://" in url_str else f"https://{url_str}"
                        shot_res = _take_playwright_screenshot(normalized_url)
                        if "screenshot_path" in shot_res:
                            screenshot_path = shot_res["screenshot_path"]
                            temp_screenshot_path = screenshot_path
                    except Exception as exc:
                        print(f"  Warning: Playwright screenshot failed: {exc}")
            except Exception as exc:
                web_collector_error = str(exc)

        if manual_str:
            web_markdown = manual_str
            source_provider = "manual_evidence"

        if web_markdown:
            distilled = ContentDistiller().distill(
                web_markdown,
                source_url=url_str or "manual",
                source_provider=source_provider,
            )
            content_distillation_summary = distilled.to_summary()
            if distilled.selected_text:
                web_markdown = distilled.selected_text

        visual_semantics = {"status": "not_detected", "data": {}}
        if screenshot_path:
            try:
                visual_semantics = analyze_visual_semantics(screenshot_path, brand_name)
            except Exception:
                visual_semantics = {"status": "unavailable", "data": {}}

        try:
            if self.llm is not None and getattr(self.llm, "api_key", None):
                try:
                    result = self._extract_via_llm(
                        web_markdown, visual_semantics, brand_name, url_str or "manual"
                    )
                    if result:
                        if content_distillation_summary:
                            result["content_distillation_summary"] = content_distillation_summary
                        self._mark_legacy_direct_result(result, source_provider)
                        return result
                except Exception:
                    pass

            result = self._extract_via_heuristic(
                web_markdown,
                visual_semantics,
                brand_name,
                url_str or "manual",
                web_collector_error,
                content_distillation_summary,
            )
            self._mark_legacy_direct_result(result, source_provider)
            return result
        finally:
            if temp_screenshot_path:
                try:
                    if os.path.exists(temp_screenshot_path):
                        os.remove(temp_screenshot_path)
                except Exception:
                    pass

    def extract_from_audit_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Build a Magnetism scan from an existing Brand Audit run snapshot.

        This is the preferred integration path: Brand Audit owns collection,
        evidence normalization, confidence, and degraded-state handling.
        """
        canonical_evidence = build_canonical_brand_evidence(snapshot)
        recommended_research_pack = self._build_research_pack(snapshot)
        research_pack = recommended_research_pack.pack
        contextdev_candidate_summary = self._contextdev_candidate_summary_from_snapshot(snapshot)
        brand_name = canonical_evidence.brand_name
        url = canonical_evidence.url
        strategic_packet = canonical_evidence.strategic_packet
        evidence_text = canonical_evidence.interpreter_text
        visual_semantics = canonical_evidence.visual_semantics
        evidence_packet_summary = canonical_evidence.to_summary()

        if self.llm is not None and getattr(self.llm, "api_key", None):
            try:
                result = self._extract_via_llm(evidence_text, visual_semantics, brand_name, url)
                if result:
                    return self._enrich_result_from_audit_snapshot(
                        result=result,
                        canonical_evidence=canonical_evidence,
                        strategic_packet=strategic_packet,
                        evidence_packet_summary=evidence_packet_summary,
                        recommended_research_pack=recommended_research_pack,
                        research_pack=research_pack,
                        contextdev_candidate_summary=contextdev_candidate_summary,
                        enrich_layers_from_packet=True,
                    )
            except Exception:
                pass

        result = self._extract_via_heuristic(
            evidence_text,
            visual_semantics,
            brand_name,
            url,
            collector_error="",
            strategic_evidence_packet=strategic_packet.to_dict(),
        )
        return self._enrich_result_from_audit_snapshot(
            result=result,
            canonical_evidence=canonical_evidence,
            strategic_packet=strategic_packet,
            evidence_packet_summary=evidence_packet_summary,
            recommended_research_pack=recommended_research_pack,
            research_pack=research_pack,
            contextdev_candidate_summary=contextdev_candidate_summary,
            enrich_layers_from_packet=True,
        )

    def _enrich_result_from_audit_snapshot(
        self,
        *,
        result: dict[str, Any],
        canonical_evidence: Any,
        strategic_packet: StrategicEvidencePacket,
        evidence_packet_summary: dict[str, Any],
        recommended_research_pack: RecommendedResearchPack,
        research_pack: Any | None,
        contextdev_candidate_summary: dict[str, Any] | None,
        enrich_layers_from_packet: bool,
    ) -> dict[str, Any]:
        packet_dict = strategic_packet.to_dict()
        brand_name = canonical_evidence.brand_name
        url = canonical_evidence.url

        result["source_run_id"] = canonical_evidence.run_id
        result["source"] = "brand_audit_snapshot"
        result["extraction_mode"] = CANONICAL_EXTRACTION_MODE
        result["canonical_evidence_source"] = "brand_audit_snapshot"
        result["llm_model_roles"] = self._llm_model_roles()
        result["limitations"].extend(canonical_evidence.limitations)
        result["evidence_packet_summary"] = evidence_packet_summary
        result.update(recommended_research_pack.metadata_payload())
        result["strategic_evidence_packet"] = packet_dict
        if enrich_layers_from_packet:
            self._enrich_layers_from_strategic_packet(
                result["magenta_circle"],
                packet_dict,
                replace_detected_ambientspace=True,
            )
        brand_context_brief = build_brand_context_brief(
            brand_name=brand_name,
            url=url,
            layers=result["magenta_circle"],
            strategic_packet=packet_dict,
        ).to_dict()
        result["brand_context_brief"] = brand_context_brief
        result["tldr_brand3"] = self._derive_tldr(result["magenta_circle"], packet_dict, brand_context_brief)
        self._apply_tldr_generation_flow(
            result=result,
            brand_name=brand_name,
            url=url,
            packet_dict=packet_dict,
            brand_context_brief=brand_context_brief,
            research_pack=research_pack,
            contextdev_candidate_summary=contextdev_candidate_summary,
        )
        result.setdefault("tldr_generation_mode", "legacy_code")
        result["metrics"] = self._derive_metrics(
            result["magenta_circle"],
            result["tldr_brand3"],
            scoring_context=(
                result.get("analyst_tldr_validated", {}).get("scoring_context")
                if isinstance(result.get("analyst_tldr_validated"), dict)
                else None
            ),
        )
        result["diagnosis"] = self._derive_diagnosis(result["magenta_circle"], result["metrics"])
        result["system_reading"] = self._build_system_reading(
            tldr=result["tldr_brand3"],
            layers=result["magenta_circle"],
            metrics=result["metrics"],
            url=url,
            brand_name=brand_name,
            evidence_packet_summary=evidence_packet_summary,
        )
        self._add_legacy_fields(result)
        return result

    @staticmethod
    def _build_research_pack(snapshot: dict[str, Any]) -> RecommendedResearchPack:
        return build_recommended_research_pack(
            snapshot,
            allow_graph=BRAND3_BRAND_RESEARCH_GRAPH_PACK,
        )

    def _apply_tldr_generation_flow(
        self,
        *,
        result: dict[str, Any],
        brand_name: str,
        url: str,
        packet_dict: dict[str, Any],
        brand_context_brief: dict[str, Any],
        research_pack: Any | None = None,
        contextdev_candidate_summary: dict[str, Any] | None = None,
    ) -> None:
        result["research_pack"] = research_pack.to_dict() if hasattr(research_pack, "to_dict") else research_pack
        self._apply_research_pack_quality_diagnostic(result=result, research_pack=research_pack)
        self._apply_contextdev_visual_enrichment_shadow(
            result=result,
            research_pack=research_pack,
            candidate_summary=contextdev_candidate_summary,
        )
        if BRAND3_MAGNETISM_RESEARCH_PACK_TLDR:
            self._apply_research_pack_tldr(
                result=result,
                brand_name=brand_name,
                url=url,
                packet_dict=packet_dict,
                brand_context_brief=brand_context_brief,
                research_pack=research_pack,
            )
            return

    @staticmethod
    def _apply_research_pack_quality_diagnostic(
        *,
        result: dict[str, Any],
        research_pack: Any | None,
    ) -> None:
        if research_pack is None:
            result["research_pack_quality"] = {
                "status": "skipped",
                "reason": "missing_research_pack",
            }
            return

        try:
            quality = evaluate_research_pack_quality(research_pack)
            quality_payload = quality.to_dict()
            quality_payload["gate"] = evaluate_research_pack_quality_gate(quality)
            result["research_pack_quality"] = quality_payload
        except Exception as exc:
            result["research_pack_quality"] = {
                "status": "error",
                "reason": str(exc),
            }

    @staticmethod
    def _contextdev_candidate_summary_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
        summary = snapshot.get("contextdev_candidate_summary")
        if isinstance(summary, dict):
            return summary

        contextdev = snapshot.get("contextdev")
        if isinstance(contextdev, dict):
            summary = contextdev.get("candidate_summary")
            if isinstance(summary, dict):
                return summary

        for raw_input in reversed(snapshot.get("raw_inputs") or []):
            if not isinstance(raw_input, dict):
                continue
            if raw_input.get("source") != "contextdev_candidate_summary":
                continue
            payload = raw_input.get("payload")
            if isinstance(payload, dict):
                return payload
        return None

    @staticmethod
    def _apply_contextdev_visual_enrichment_shadow(
        *,
        result: dict[str, Any],
        research_pack: Any | None,
        candidate_summary: dict[str, Any] | None,
    ) -> None:
        if not BRAND3_CONTEXTDEV_VISUAL_ENRICHMENT:
            return

        if research_pack is None:
            result["contextdev_visual_enrichment_shadow"] = {
                "status": "skipped",
                "reason": "missing_research_pack",
            }
            return

        if not isinstance(candidate_summary, dict):
            result["contextdev_visual_enrichment_shadow"] = {
                "status": "skipped",
                "reason": "missing_candidate_summary",
            }
            return

        try:
            dry_run = build_contextdev_research_pack_dry_run(
                research_pack,
                candidate_summary,
            ).to_dict()
        except Exception as exc:
            result["contextdev_visual_enrichment_shadow"] = {
                "status": "error",
                "reason": str(exc),
            }
            return

        enriched_pack = dry_run.get("enriched_pack", {})
        if not isinstance(enriched_pack, dict):
            enriched_pack = {}
        field_updates = dry_run.get("field_updates") or []
        if not isinstance(field_updates, list):
            field_updates = list(field_updates or [])
        visual_or_conceptual_delta = [
            str(update.get("value") or "")
            for update in field_updates
            if isinstance(update, dict)
            and update.get("field") == "visual_or_conceptual_signals"
            and isinstance(update.get("value"), str)
        ]
        product_summary_delta = next(
            (
                str(update.get("value") or "")
                for update in field_updates
                if isinstance(update, dict)
                and update.get("field") == "product_summary"
                and isinstance(update.get("value"), str)
            ),
            None,
        )
        result["contextdev_visual_enrichment_shadow"] = {
            "status": "evaluated",
            "version": dry_run.get("version"),
            "field_updates": dry_run.get("field_updates") or [],
            "promotion_report": dry_run.get("promotion_report") or {},
            "enriched_pack_delta": {
                "visual_or_conceptual_signals": visual_or_conceptual_delta,
                "product_summary": product_summary_delta or enriched_pack.get("product_summary"),
                "confidence_notes": enriched_pack.get("confidence_notes", []),
            },
        }

    def _apply_research_pack_tldr(
        self,
        *,
        result: dict[str, Any],
        brand_name: str,
        url: str,
        packet_dict: dict[str, Any],
        brand_context_brief: dict[str, Any],
        research_pack: Any | None = None,
    ) -> None:
        current_tldr = result.get("tldr_brand3")
        if not isinstance(current_tldr, dict):
            return

        if self.analyst_llm is None or not getattr(self.analyst_llm, "api_key", None):
            result["legacy_tldr_brand3"] = current_tldr
            result["tldr_generation_mode"] = "legacy_fallback_no_llm"
            result.setdefault("warnings", []).append(
                "Analyst Pass disabled because no LLM API key is available; legacy TLDR preserved."
            )
            return

        try:
            run = _resolve_run_analyst_tldr_pass(
                llm=self.analyst_llm,
                brand_name=brand_name,
                url=url,
                research_pack=research_pack or brand_context_brief,
                current_tldr=current_tldr,
            )
        except Exception as exc:
            result["legacy_tldr_brand3"] = current_tldr
            result["tldr_generation_mode"] = "legacy_fallback_llm_error"
            result.setdefault("warnings", []).append(f"Analyst Pass failed; legacy TLDR preserved. error={exc}")
            return

        result["legacy_tldr_brand3"] = current_tldr
        result["analyst_tldr_raw"] = run.get("raw") or {}
        result["analyst_tldr_validated"] = run.get("validated") or {}
        result["analyst_tldr_analysis_error"] = run.get("analysis_error")

        if run.get("analysis_error"):
            result["tldr_generation_mode"] = "legacy_fallback_llm_error"
            warning = run["analysis_error"].get("detail") if isinstance(run["analysis_error"], dict) else "Analyst Pass failed."
            result.setdefault("warnings", []).append(f"Analyst Pass fallback: {warning}")
            result["tldr_brand3"] = current_tldr
            result["tldr_strategy"] = {
                "mode": "llm_analyst_pass_fallback",
                "validation_notes": [],
                "validation_warnings": [],
                "degraded_fields": [],
            }
            return

        validated = run.get("validated") if isinstance(run.get("validated"), dict) else {}
        tldr = validated.get("tldr_brand3") if isinstance(validated, dict) else {}
        if not isinstance(tldr, dict) or not tldr:
            result["tldr_generation_mode"] = "legacy_fallback_llm_error"
            result.setdefault("warnings", []).append("Analyst Pass returned no usable TLDR; legacy TLDR preserved.")
            result["tldr_brand3"] = current_tldr
            return

        result["tldr_brand3"] = tldr
        layers = result.get("magenta_circle")
        if isinstance(layers, dict):
            self._sync_layers_from_tldr(layers, result["tldr_brand3"])
        result["tldr_generation_mode"] = "analyst_pass_validated"
        result["tldr_strategy"] = {
            "mode": "llm_analyst_pass",
            "prompt_version": validated.get("prompt_version"),
            "validation_notes": validated.get("validation_notes") or [],
            "validation_warnings": validated.get("validation_warnings") or [],
            "degraded_fields": validated.get("degraded_fields") or [],
        }
