CREATE TABLE b3s_history.capture_fingerprints (
    capture_id uuid PRIMARY KEY REFERENCES b3s_history.captures(id) ON DELETE CASCADE,
    schema_version text NOT NULL,
    material_fingerprint text NOT NULL CHECK (length(material_fingerprint) = 64),
    semantic_fingerprint text NOT NULL CHECK (length(semantic_fingerprint) = 64),
    component_fingerprints jsonb NOT NULL DEFAULT '{}'::jsonb,
    acquisition_profile jsonb NOT NULL DEFAULT '{}'::jsonb,
    computed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE b3s_history.evaluation_comparisons (
    evaluation_run_id uuid PRIMARY KEY REFERENCES b3s_history.evaluation_runs(id) ON DELETE CASCADE,
    baseline_evaluation_run_id uuid REFERENCES b3s_history.evaluation_runs(id) ON DELETE SET NULL,
    previous_evaluation_run_id uuid REFERENCES b3s_history.evaluation_runs(id) ON DELETE SET NULL,
    schema_version text NOT NULL,
    policy_version text NOT NULL,
    classification text NOT NULL,
    canonical_status text NOT NULL,
    reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
    assessment jsonb NOT NULL DEFAULT '{}'::jsonb,
    computed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE b3s_history.brand_canonical_selections (
    brand_id uuid PRIMARY KEY REFERENCES b3s_history.brands(id) ON DELETE CASCADE,
    evaluation_run_id uuid NOT NULL REFERENCES b3s_history.evaluation_runs(id) ON DELETE CASCADE,
    status text NOT NULL CHECK (status IN ('canonical', 'provisional')),
    policy_version text NOT NULL,
    selected_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_b3s_comparisons_classification
    ON b3s_history.evaluation_comparisons (classification);
CREATE INDEX idx_b3s_canonical_evaluation
    ON b3s_history.brand_canonical_selections (evaluation_run_id);
