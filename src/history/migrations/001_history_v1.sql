CREATE TABLE b3s_history.workspaces (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]*$'),
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE b3s_history.brands (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES b3s_history.workspaces(id) ON DELETE CASCADE,
    canonical_domain text NOT NULL,
    display_name text NOT NULL,
    canonical_url text NOT NULL,
    first_observed_at timestamptz NOT NULL,
    latest_observed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, canonical_domain)
);

CREATE TABLE b3s_history.scan_runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES b3s_history.workspaces(id) ON DELETE CASCADE,
    brand_id uuid NOT NULL REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    source_scan_id text NOT NULL,
    source_run_id text NOT NULL DEFAULT '',
    status text NOT NULL,
    pipeline_version text NOT NULL,
    acquisition_state text NOT NULL DEFAULT 'unknown',
    requested_at timestamptz NOT NULL,
    started_at timestamptz,
    completed_at timestamptz,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    error_summary text NOT NULL DEFAULT '',
    request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (workspace_id, source_scan_id)
);

CREATE TABLE b3s_history.captures (
    id uuid PRIMARY KEY,
    scan_run_id uuid NOT NULL UNIQUE REFERENCES b3s_history.scan_runs(id) ON DELETE CASCADE,
    brand_id uuid NOT NULL REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    observed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    source_url text NOT NULL,
    content_hash text NOT NULL CHECK (length(content_hash) = 64),
    acquisition_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    limitations jsonb NOT NULL DEFAULT '[]'::jsonb,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE b3s_history.evidence_records (
    id uuid PRIMARY KEY,
    capture_id uuid NOT NULL REFERENCES b3s_history.captures(id) ON DELETE CASCADE,
    evidence_ref text NOT NULL,
    source text NOT NULL,
    source_class text NOT NULL DEFAULT 'other',
    evidence_type text NOT NULL,
    url text NOT NULL DEFAULT '',
    content text NOT NULL,
    content_raw bytea,
    content_hash text NOT NULL CHECK (length(content_hash) = 64),
    confidence text NOT NULL DEFAULT 'medium',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (capture_id, evidence_ref)
);

CREATE TABLE b3s_history.evaluation_runs (
    id uuid PRIMARY KEY,
    capture_id uuid NOT NULL REFERENCES b3s_history.captures(id) ON DELETE CASCADE,
    source_evaluation_key text NOT NULL,
    status text NOT NULL,
    pipeline_version text NOT NULL,
    rubric_version text NOT NULL,
    prompt_version text NOT NULL,
    evaluator_model text NOT NULL,
    gate_authority text NOT NULL DEFAULT 'unknown',
    evaluated_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    score numeric(8,3),
    base_average numeric(8,3),
    reliability_status text NOT NULL DEFAULT 'unknown',
    limitations jsonb NOT NULL DEFAULT '[]'::jsonb,
    not_detected jsonb NOT NULL DEFAULT '[]'::jsonb,
    config jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (capture_id, source_evaluation_key)
);

CREATE TABLE b3s_history.block_interpretations (
    id uuid PRIMARY KEY,
    evaluation_run_id uuid NOT NULL REFERENCES b3s_history.evaluation_runs(id) ON DELETE CASCADE,
    component_key text NOT NULL,
    detected boolean NOT NULL,
    content text NOT NULL DEFAULT '',
    rejected_content text NOT NULL DEFAULT '',
    confidence text NOT NULL DEFAULT '',
    rationale text NOT NULL DEFAULT '',
    coverage_status text NOT NULL DEFAULT '',
    provenance_source text NOT NULL DEFAULT '',
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (evaluation_run_id, component_key)
);

CREATE TABLE b3s_history.block_evidence_links (
    block_interpretation_id uuid NOT NULL REFERENCES b3s_history.block_interpretations(id) ON DELETE CASCADE,
    evidence_record_id uuid NOT NULL REFERENCES b3s_history.evidence_records(id) ON DELETE RESTRICT,
    ordinal smallint NOT NULL DEFAULT 0,
    PRIMARY KEY (block_interpretation_id, evidence_record_id)
);

CREATE TABLE b3s_history.component_evaluations (
    id uuid PRIMARY KEY,
    evaluation_run_id uuid NOT NULL REFERENCES b3s_history.evaluation_runs(id) ON DELETE CASCADE,
    block_interpretation_id uuid REFERENCES b3s_history.block_interpretations(id) ON DELETE SET NULL,
    component_key text NOT NULL,
    label text NOT NULL,
    status text NOT NULL,
    score numeric(8,3),
    max_score numeric(8,3),
    points numeric(8,3),
    confidence text NOT NULL DEFAULT '',
    detected_content text NOT NULL DEFAULT '',
    summary text NOT NULL DEFAULT '',
    verdict text NOT NULL DEFAULT '',
    message text NOT NULL DEFAULT '',
    detection_mode text NOT NULL DEFAULT '',
    detection_limitations jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence_source_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (evaluation_run_id, component_key)
);

CREATE TABLE b3s_history.tile_verdicts (
    id uuid PRIMARY KEY,
    component_evaluation_id uuid NOT NULL REFERENCES b3s_history.component_evaluations(id) ON DELETE CASCADE,
    evidence_record_id uuid REFERENCES b3s_history.evidence_records(id) ON DELETE SET NULL,
    tile_id text NOT NULL,
    state text NOT NULL CHECK (state IN ('ok', 'no', 'sin_evidencia')),
    evidence_ref text NOT NULL DEFAULT '',
    evidence_quote text NOT NULL DEFAULT '',
    evidence_literal_verified boolean,
    reason text NOT NULL DEFAULT '',
    required_context text NOT NULL DEFAULT '',
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (component_evaluation_id, tile_id)
);

CREATE TABLE b3s_history.acquisition_attempts (
    id uuid PRIMARY KEY,
    capture_id uuid NOT NULL REFERENCES b3s_history.captures(id) ON DELETE CASCADE,
    provider text NOT NULL,
    intent text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT '',
    detail text NOT NULL DEFAULT '',
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE b3s_history.artifacts (
    id uuid PRIMARY KEY,
    capture_id uuid NOT NULL REFERENCES b3s_history.captures(id) ON DELETE CASCADE,
    kind text NOT NULL,
    source text NOT NULL DEFAULT '',
    uri text NOT NULL DEFAULT '',
    content_hash text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (content_hash IS NULL OR length(content_hash) = 64)
);

CREATE TABLE b3s_history.report_snapshots (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES b3s_history.workspaces(id) ON DELETE CASCADE,
    evaluation_run_id uuid NOT NULL REFERENCES b3s_history.evaluation_runs(id) ON DELETE CASCADE,
    source_report_id text NOT NULL,
    format text NOT NULL DEFAULT 'b3s-report-json',
    language text NOT NULL DEFAULT 'es',
    created_at timestamptz NOT NULL,
    payload_sha256 text NOT NULL CHECK (length(payload_sha256) = 64),
    payload jsonb NOT NULL,
    payload_raw bytea NOT NULL,
    UNIQUE (workspace_id, source_report_id),
    UNIQUE (evaluation_run_id, format, language)
);

CREATE INDEX idx_b3s_brands_workspace_latest
    ON b3s_history.brands (workspace_id, latest_observed_at DESC);
CREATE INDEX idx_b3s_scan_runs_brand_recorded
    ON b3s_history.scan_runs (brand_id, recorded_at DESC);
CREATE INDEX idx_b3s_captures_brand_observed
    ON b3s_history.captures (brand_id, observed_at DESC);
CREATE INDEX idx_b3s_evidence_capture_type
    ON b3s_history.evidence_records (capture_id, evidence_type);
CREATE INDEX idx_b3s_evidence_source_class
    ON b3s_history.evidence_records (source_class);
CREATE INDEX idx_b3s_evaluations_capture_time
    ON b3s_history.evaluation_runs (capture_id, evaluated_at DESC);
CREATE INDEX idx_b3s_components_key_status
    ON b3s_history.component_evaluations (component_key, status);
CREATE INDEX idx_b3s_tiles_state
    ON b3s_history.tile_verdicts (state, tile_id);
CREATE INDEX idx_b3s_reports_created
    ON b3s_history.report_snapshots (workspace_id, created_at DESC);
CREATE INDEX idx_b3s_report_payload_gin
    ON b3s_history.report_snapshots USING gin (payload jsonb_path_ops);

CREATE VIEW b3s_history.brand_history AS
WITH ranked_evaluations AS (
    SELECT
        evaluation_runs.*,
        row_number() OVER (
            PARTITION BY evaluation_runs.capture_id
            ORDER BY evaluation_runs.evaluated_at DESC, evaluation_runs.recorded_at DESC
        ) AS evaluation_rank
    FROM b3s_history.evaluation_runs
    WHERE evaluation_runs.status = 'completed'
)
SELECT
    workspaces.id AS workspace_id,
    workspaces.slug AS workspace_slug,
    brands.id AS brand_id,
    brands.canonical_domain,
    brands.display_name,
    brands.canonical_url,
    scan_runs.id AS scan_run_id,
    scan_runs.source_scan_id,
    captures.id AS capture_id,
    captures.observed_at,
    captures.recorded_at AS capture_recorded_at,
    ranked_evaluations.id AS evaluation_run_id,
    ranked_evaluations.evaluated_at,
    ranked_evaluations.rubric_version,
    ranked_evaluations.prompt_version,
    ranked_evaluations.evaluator_model,
    ranked_evaluations.score,
    ranked_evaluations.base_average,
    ranked_evaluations.reliability_status,
    report_snapshots.source_report_id,
    report_snapshots.payload_sha256
FROM ranked_evaluations
JOIN b3s_history.captures ON captures.id = ranked_evaluations.capture_id
JOIN b3s_history.scan_runs ON scan_runs.id = captures.scan_run_id
JOIN b3s_history.brands ON brands.id = captures.brand_id
JOIN b3s_history.workspaces ON workspaces.id = brands.workspace_id
LEFT JOIN b3s_history.report_snapshots
    ON report_snapshots.evaluation_run_id = ranked_evaluations.id
WHERE ranked_evaluations.evaluation_rank = 1;

CREATE VIEW b3s_history.brand_current_state AS
SELECT current_rows.*
FROM (
    SELECT
        brand_history.*,
        row_number() OVER (
            PARTITION BY brand_history.workspace_id, brand_history.brand_id
            ORDER BY brand_history.observed_at DESC, brand_history.evaluated_at DESC
        ) AS brand_state_rank
    FROM b3s_history.brand_history
) AS current_rows
WHERE current_rows.brand_state_rank = 1;
