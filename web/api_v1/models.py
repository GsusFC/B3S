"""Pydantic contracts for the B3S Scanner API v1."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Language = Literal["es"]
ScanStatus = Literal["running", "blocked", "completed", "failed", "cancelled"]
EvidenceAdjudicationDecision = Literal[
    "accepted",
    "disputed",
    "rejected",
    "revoked",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScanCreateRequest(StrictModel):
    url: str = Field(min_length=4, max_length=2048, examples=["https://example.com"])
    brand_name: str | None = Field(default=None, max_length=200)
    language: Language = "es"
    allow_degraded_fallback: bool = False

    @field_validator("url", "brand_name", mode="before")
    @classmethod
    def strip_strings(cls, value):
        return value.strip() if isinstance(value, str) else value


class ScanPhase(StrictModel):
    key: str
    label: str
    state: str


class ScanLinks(StrictModel):
    self: str
    result: str
    evidence: str
    continue_scan: str
    cancel: str
    report: str
    report_markdown: str


class ScanFailure(StrictModel):
    code: str
    message: str
    retryable: bool = False


class ScanStatusResponse(StrictModel):
    object: Literal["scan"] = "scan"
    api_version: Literal["v1"] = "v1"
    id: str
    status: ScanStatus
    phase: str
    progress: float = Field(ge=0, le=1)
    brand_name: str
    url: str
    language: Language = "es"
    started_at: str | None = None
    completed_at: str | None = None
    phases: list[ScanPhase] = Field(default_factory=list)
    acquisition: list[dict[str, Any]] = Field(default_factory=list)
    acquisition_gate: dict[str, Any] = Field(default_factory=dict)
    failure: ScanFailure | None = None
    result_available: bool
    durable_status: bool = True
    resumable_after_restart: bool = False
    links: ScanLinks


class EvidenceReference(StrictModel):
    ref: str
    component: str
    url: str
    snippet: str


class TileSummary(StrictModel):
    passed: int = 0
    failed: int = 0
    insufficient_evidence: int = 0
    total: int = 0


class ScanComponent(StrictModel):
    key: str
    label: str
    status: str
    score: float | int | None = None
    max_score: float | int | None = None
    confidence: str
    summary: str
    verdict: str
    message: str
    detected_content: str
    coverage_status: str
    tile_summary: TileSummary
    tiles: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[EvidenceReference] = Field(default_factory=list)


class ScanBrand(StrictModel):
    name: str
    url: str
    domain: str


class ScanScore(StrictModel):
    value: float | int | None = None
    scale: int = 100
    base_average: float | int | None = None
    reliability_status: str


class ResultMetadata(StrictModel):
    schema_version: Literal["b3s-scanner-result-v1"] = "b3s-scanner-result-v1"
    pipeline_schema_version: str
    pipeline_commit_sha: str = "unknown"
    rubric_version: str
    prompt_version: str
    evaluator_model: str
    generated_at: str | None = None


class ScanResultResponse(StrictModel):
    object: Literal["scan_result"] = "scan_result"
    api_version: Literal["v1"] = "v1"
    id: str
    status: Literal["completed"] = "completed"
    brand: ScanBrand
    score: ScanScore
    summary: str
    executive_reading: str = ""
    components: list[ScanComponent]
    detected_count: int
    component_count: int
    not_detected: list[str]
    insufficient_evidence: list[str] = Field(default_factory=list)
    limitations: list[str]
    acquisition_summary: dict[str, Any]
    acquisition_gate: dict[str, Any]
    stability: dict[str, Any] = Field(default_factory=dict)
    metadata: ResultMetadata
    links: ScanLinks


class ScanEvidenceResponse(StrictModel):
    object: Literal["scan_evidence"] = "scan_evidence"
    api_version: Literal["v1"] = "v1"
    scan_id: str
    brand: ScanBrand
    acquisition_summary: dict[str, Any]
    acquisition_gate: dict[str, Any]
    references: list[EvidenceReference]
    absences: list[dict[str, Any]]
    attempts: list[dict[str, Any]]
    totals: dict[str, int]
    links: ScanLinks


class Pagination(StrictModel):
    limit: int
    offset: int
    count: int
    has_more: bool


class ScanHistoryItem(StrictModel):
    id: str
    status: Literal["completed"] = "completed"
    brand_name: str
    url: str
    score: float | int | None = None
    created_at: str | None = None
    reliability_status: str = "unknown"
    canonical_status: str = "unknown"
    stability_classification: str = "unknown"
    stability_reason_codes: list[str] = Field(default_factory=list)
    result_url: str
    report_url: str


class BrandScanHistoryResponse(StrictModel):
    object: Literal["scan_list"] = "scan_list"
    api_version: Literal["v1"] = "v1"
    domain: str
    items: list[ScanHistoryItem]
    canonical_report_id: str | None = None
    provisional_report_id: str | None = None
    selected_report_id: str | None = None
    pagination: Pagination


class EvidenceLedgerShadowResponse(StrictModel):
    object: Literal["evidence_ledger_shadow"] = "evidence_ledger_shadow"
    api_version: Literal["v1"] = "v1"
    domain: str
    schema_version: str
    policy_version: str
    mode: Literal["disabled", "shadow"]
    runtime_effect: Literal[False] = False
    state_fingerprint: str
    brand: dict[str, Any] = Field(default_factory=dict)
    report_count: int = 0
    latest_report_id: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    persistence: dict[str, Any] = Field(default_factory=dict)


class EvidenceMemoryIdentityV2ShadowResponse(StrictModel):
    object: Literal["evidence_memory_identity_v2_shadow"] = (
        "evidence_memory_identity_v2_shadow"
    )
    api_version: Literal["v1"] = "v1"
    domain: str
    schema_version: str
    policy_version: str
    mode: Literal["disabled", "shadow"]
    runtime_effect: Literal[False] = False
    authority: Literal[False] = False
    state_fingerprint: str
    brand: dict[str, Any] = Field(default_factory=dict)
    report_count: int = 0
    latest_report_id: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    source_independence: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    adjudication: dict[str, Any] = Field(default_factory=dict)
    persistence: dict[str, Any] = Field(default_factory=dict)


class EvidenceClaimMemoryShadowResponse(StrictModel):
    object: Literal["evidence_claim_memory_shadow"] = (
        "evidence_claim_memory_shadow"
    )
    api_version: Literal["v1"] = "v1"
    domain: str
    schema_version: str
    policy_version: str
    mode: Literal["disabled", "shadow"]
    runtime_effect: Literal[False] = False
    authority: Literal[False] = False
    state_fingerprint: str
    brand: dict[str, Any] = Field(default_factory=dict)
    report_count: int = 0
    latest_report_id: str | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    slots: list[dict[str, Any]] = Field(default_factory=list)
    variants: list[dict[str, Any]] = Field(default_factory=list)
    occurrences: list[dict[str, Any]] = Field(default_factory=list)
    persistence: dict[str, Any] = Field(default_factory=dict)


class EvidenceMemoryAdjudicationCreateRequest(StrictModel):
    subject_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: EvidenceAdjudicationDecision
    expected_current_event_id: str | None = Field(
        ...,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    )
    reason_code: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_]*$",
    )
    rationale: str = Field(min_length=1, max_length=2000)
    evaluator_version: str = Field(min_length=1, max_length=200)

    @field_validator(
        "subject_id",
        "expected_current_event_id",
        "reason_code",
        "rationale",
        "evaluator_version",
        mode="before",
    )
    @classmethod
    def strip_adjudication_strings(cls, value):
        return value.strip() if isinstance(value, str) else value


class EvidenceMemoryAdjudicationEvent(StrictModel):
    id: str
    subject_type: Literal["evidence"] = "evidence"
    subject_id: str
    sequence: int = Field(ge=1)
    decision: EvidenceAdjudicationDecision
    effective_state: Literal[
        "accepted",
        "disputed",
        "rejected",
        "revoked",
        "superseded",
    ]
    supersedes_event_id: str | None = None
    schema_version: str
    policy_version: str
    evaluator_version: str
    reviewer: str
    actor_id: str
    reason_code: str
    rationale: str
    runtime_effect: Literal[False] = False
    authority: Literal[False] = False
    created_at: str


class EvidenceMemoryAdjudicationCreateResponse(StrictModel):
    object: Literal["evidence_memory_adjudication"] = (
        "evidence_memory_adjudication"
    )
    api_version: Literal["v1"] = "v1"
    domain: str
    replayed: bool
    runtime_effect: Literal[False] = False
    authority: Literal[False] = False
    event: EvidenceMemoryAdjudicationEvent


class EvidenceMemoryAdjudicationJournalResponse(StrictModel):
    object: Literal["evidence_memory_adjudication_list"] = (
        "evidence_memory_adjudication_list"
    )
    api_version: Literal["v1"] = "v1"
    domain: str
    runtime_effect: Literal[False] = False
    authority: Literal[False] = False
    events: list[EvidenceMemoryAdjudicationEvent] = Field(default_factory=list)
    current: list[EvidenceMemoryAdjudicationEvent] = Field(default_factory=list)
    pagination: Pagination


class ApiCapabilitiesResponse(StrictModel):
    object: Literal["api_capabilities"] = "api_capabilities"
    api_version: Literal["v1"] = "v1"
    result_schema_version: Literal["b3s-scanner-result-v1"] = "b3s-scanner-result-v1"
    authentication: Literal["bearer"] = "bearer"
    idempotency: Literal["durable"] = "durable"
    job_status_durability: Literal["durable"] = "durable"
    job_execution: Literal["process_bound"] = "process_bound"
    restart_behavior: Literal["marks_incomplete_as_failed"] = "marks_incomplete_as_failed"
    supported_languages: list[Language] = Field(default_factory=lambda: ["es"])


class ApiErrorBody(StrictModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] | None = None


class ApiErrorResponse(StrictModel):
    error: ApiErrorBody
