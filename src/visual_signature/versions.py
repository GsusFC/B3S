"""Central registry of Visual Signature schema and prompt versions.

All version constants live here so they can be discovered, audited, and
cross-referenced in one place. Submodules import their version from here
and keep using it locally — this file is the single source of truth.
"""

from __future__ import annotations

# --- Top-level contracts ---
VISUAL_SIGNATURE_SCAN_VERSION = "visual-signature-scan-v1"
VISUAL_SIGNATURE_EVIDENCE_VERSION = "visual-signature-evidence-v1"
VISUAL_SIGNATURE_PLATFORM_SCHEMA_VERSION = "brand3-platform-1"
VISUAL_DIAGNOSIS_SCHEMA_VERSION = "visual-diagnosis-v1"
VISUAL_EVIDENCE_PACK_SCHEMA_VERSION = "visual-evidence-pack-v1"
VISUAL_DIAGNOSIS_CLEAN_CAPTURE_DECISION_SCHEMA_VERSION = "visual-diagnosis-clean-capture-decision-1"
VISUAL_INTERPRETATION_SCHEMA_VERSION = "visual-interpretation-v1"

# --- Prompt versions (two distinct prompts, two distinct constants) ---
ANNOTATION_PROMPT_VERSION = "visual-signature-annotation-prompt-1"
MULTIMODAL_PROMPT_VERSION = "visual-signature-multimodal-v3"

# --- Reviewer viewer / workflow ---
REVIEWER_VIEWER_SCHEMA_VERSION = "visual-signature-reviewer-viewer-1"
REVIEWER_WORKFLOW_PILOT_SCHEMA_VERSION = "visual-signature-reviewer-workflow-pilot-1"

# --- Corpus expansion ---
CORPUS_EXPANSION_QUEUE_ITEM_SCHEMA_VERSION = "visual-signature-corpus-expansion-queue-item-1"
CORPUS_EXPANSION_REVIEW_QUEUE_SCHEMA_VERSION = "visual-signature-corpus-expansion-review-queue-1"
CORPUS_EXPANSION_METRICS_SCHEMA_VERSION = "visual-signature-corpus-expansion-metrics-1"
CORPUS_EXPANSION_MANIFEST_SCHEMA_VERSION = "visual-signature-corpus-expansion-manifest-1"
CORPUS_EXPANSION_READINESS_SCHEMA_VERSION = "visual-signature-corpus-expansion-readiness-1"

# --- Governance ---
CAPABILITY_REGISTRY_SCHEMA_VERSION = "visual-signature-capability-registry-1"
RUNTIME_POLICY_MATRIX_SCHEMA_VERSION = "visual-signature-runtime-policy-matrix-1"
THREE_TRACK_VALIDATION_PLAN_SCHEMA_VERSION = "visual-signature-three-track-validation-plan-1"
GOVERNANCE_INTEGRITY_SCHEMA_VERSION = "visual-signature-governance-integrity-1"

# --- Calibration ---
CALIBRATION_CLAIM_SCHEMA_VERSION = "visual-signature-calibration-claim-1"
CALIBRATION_GENERATED_FILE_SCHEMA_VERSION = "visual-signature-calibration-generated-file-1"
CALIBRATION_REVIEW_OUTCOME_SCHEMA_VERSION = "visual-signature-calibration-review-outcome-1"
CALIBRATION_RECORD_SCHEMA_VERSION = "visual-signature-calibration-record-1"
CALIBRATION_RECORDS_FILE_SCHEMA_VERSION = "visual-signature-calibration-records-1"
CALIBRATION_MANIFEST_SCHEMA_VERSION = "visual-signature-calibration-manifest-1"
CALIBRATION_SUMMARY_SCHEMA_VERSION = "visual-signature-calibration-summary-1"
CALIBRATION_READINESS_SCHEMA_VERSION = "visual-signature-calibration-readiness-1"

# --- Affordance semantics ---
AFFORDANCE_SEMANTICS_SCHEMA_VERSION = "visual-signature-affordance-semantics-1"
AFFORDANCE_EXPORT_SCHEMA_VERSION = "visual-signature-affordance-export-1"
AFFORDANCE_LOCALIZATION_SCHEMA_VERSION = "visual-signature-affordance-localization-1"

# --- Phase Zero (taxonomy + sub-schemas) ---
PHASE_ZERO_TAXONOMY_VERSION = "phase-zero-taxonomy-1"
OBSERVATION_REGISTRY_SCHEMA_VERSION = "phase-zero-observation-registry-1"
STATE_REGISTRY_SCHEMA_VERSION = "phase-zero-state-registry-1"
TRANSITION_REGISTRY_SCHEMA_VERSION = "phase-zero-transition-registry-1"
SCORING_REGISTRY_SCHEMA_VERSION = "phase-zero-scoring-registry-1"
UNCERTAINTY_POLICY_SCHEMA_VERSION = "phase-zero-uncertainty-policy-1"
UNCERTAINTY_PROFILE_SCHEMA_VERSION = "phase-zero-uncertainty-profile-1"
REASONING_TRACE_SCHEMA_VERSION = "phase-zero-reasoning-trace-1"
OBSERVATION_RECORD_SCHEMA_VERSION = "phase-zero-perceptual-observation-1"
STATE_RECORD_SCHEMA_VERSION = "phase-zero-perceptual-state-1"
TRANSITION_RECORD_SCHEMA_VERSION = "phase-zero-transition-record-1"
MUTATION_AUDIT_SCHEMA_VERSION = "phase-zero-mutation-audit-1"
DATASET_ELIGIBILITY_SCHEMA_VERSION = "phase-zero-dataset-eligibility-1"
REVIEW_RECORD_SCHEMA_VERSION = "phase-zero-review-record-1"
