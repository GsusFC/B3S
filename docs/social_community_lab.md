# Social/community laboratory runner

`run_social_community_lab.py` is a private laboratory composition edge. It
joins the accepted target manifest and ScrapeCreators acquisition contract to
the citation-bound social analysis contract, then writes one atomic artifact.
The legacy `b3s-social-community-lab-v1` path remains the default. Social
Tiles v2 is an explicit opt-in laboratory contract; it never changes the v1
artifact or canonical production behavior.

The lab is deliberately **not** a Scanner, Evidence Vault, or SV9 path. It
does not calculate a score, activate a tile, write a canonical state, write
SQLite/PostgreSQL, or promote evidence. Promotion starts as `insufficient` and
canonical invariance starts as `not_checked` for an independent E gate to
replace or prove.

## Social Tiles v2 (opt-in)

Use the v2 contract only when the command names it explicitly:

```bash
.venv/bin/python scripts/run_social_community_lab.py \
  --manifest examples/social_community_lab_targets.example.json \
  --acquisition-result path/to/acquisition.json \
  --analysis-contract social-tiles-v2 \
  --output out/social-community-lab/social-tiles-v2.json
```

This acquisition-result example is offline and contains no credentials or
secrets. Add `--live-analysis` only when a separately approved provider run is
intended. Without it, v2 records acquisition and marks evaluation
`not_requested`; the runner does not construct an LLM client.

### Catalog and verdict states

The catalog is fixed at six components and twelve tiles. Each component is
evaluated independently, and tile order is stable:

| Component | Tiles |
| --- | --- |
| `voice_in_action` | `ST-VI-01`, `ST-VI-02` |
| `cross_channel_consistency` | `ST-CC-01`, `ST-CC-02` |
| `listening_themes` | `ST-LT-01`, `ST-LT-02` |
| `response_behavior` | `ST-RB-01`, `ST-RB-02` |
| `reciprocity_dialogue` | `ST-RD-01`, `ST-RD-02` |
| `tension_handling` | `ST-TH-01`, `ST-TH-02` |

Every tile has exactly one scoreless state:

| State | Meaning |
| --- | --- |
| `demonstrated` | The supplied eligible evidence supports the tile, with citations. |
| `contradicted` | The supplied eligible evidence contradicts the tile, with citations. |
| `not_observed` | No qualifying semantic signal was observed in a **non-empty, validated capture set**; citations are empty. |
| `not_acquired` | Code-owned acquisition or evaluation evidence is unavailable, unsupported, or failed; citations are empty and a reason/stage are required. |

`not_observed` is never a disguise for an empty or failed acquisition. Missing,
unsupported, or failed interaction acquisition is `not_acquired`. Parent and
thread relationships use explicit validated IDs (`parent_external_id` and
`thread_external_id`); do not claim community interaction when the corresponding
roles were not acquired.

The capture set is the bounded set of admitted observations. Its observed and
fetched timestamps describe that set, not a complete historical window. A
historical window is an acquisition request bound and must not be inferred from
the capture-set ranges. Metrics are context only; semantic tile IDs and
citations remain metric-free. Provenance is receipt-bound capture context, not
tile meaning.

### Bounded analysis behavior

V2 makes at most six component calls: one one-shot call for each component with
at least one eligible tile, never a retry. A component decode/provider/validation
failure becomes local `not_acquired` verdicts for that component while valid
sibling components remain intact. Ineligible tiles are synthesized by code and
cannot be assigned a model state. The v2 artifact carries its authoritative
verdicts under `social_tiles.verdicts`; no other `state` field is admitted.

The v2 artifact is scoreless and laboratory-only. No tile affects an SV9 or
Vault score, activation, canonical assessment, persistence, or Scanner output.
Its 11 promotion gates remain `not_checked` and promotion remains
`insufficient` until an independent gate proves the evidence. Existing v1
commands, defaults, advisory candidates, and compatibility projections are
unchanged.

## Target manifest

The manifest is strict JSON. It accepts only `schema_version` and `targets`,
with the exact target shapes used by the acquisition spike:

```json
{
  "schema_version": 1,
  "targets": [
    {
      "target_id": "example-linkedin",
      "platform": "linkedin",
      "company_url": "https://www.linkedin.com/company/example"
    },
    {
      "target_id": "example-twitter",
      "platform": "twitter",
      "handle": "example"
    },
    {
      "target_id": "example-instagram",
      "platform": "instagram",
      "handle": "example"
    }
  ]
}
```

Do not put names, searches, alternate credentials, or API keys in the
manifest. LinkedIn requires an explicit `/company/<slug>` URL; Twitter and
Instagram require a bare validated handle.

## Modes

### Dry-run: no key and no network

```bash
.venv/bin/python scripts/run_social_community_lab.py \
  --manifest examples/social_community_lab_targets.example.json \
  --dry-run \
  --output out/social-community-lab/plan.json
```

Dry-run validates the manifest and all bounds, emits `request_plan` and
`capability_plan`, and reports that acquisition and analysis were not run. It
does not read `SCRAPECREATORS_API_KEY`, instantiate an HTTP client, or make a
request.

### Acquisition-only (default)

```bash
umask 077
export SCRAPECREATORS_API_KEY='set-this-in-the-process-environment-only'
.venv/bin/python scripts/run_social_community_lab.py \
  --manifest examples/social_community_lab_targets.example.json \
  --output out/social-community-lab/run.json
unset SCRAPECREATORS_API_KEY
```

The only credential source is the process environment. The artifact records
`community_analysis.status = "not_requested"`. Unsupported or missing
interactions remain explicit `not_acquired` or `partial`; they are never
represented as an empty success.

The bounded acquisition flags are passed directly to the accepted A layer:

```text
--max-post-pages N
--include-interactions
--include-replies
--max-interaction-pages N
--max-interaction-credits N
--include-raw
```

`--include-replies` requires both `--include-interactions` and an explicit
credit budget. The conservative Instagram comments-with-replies reservation is
15 credits per request. The runner never raises that ceiling, and an actual
provider overcharge remains a diagnostic in the private artifact.

### Reproducible offline analysis

An accepted acquisition result can be replayed from a local file. The
recorded analyst response may be a JSON file path, `@path`, or inline JSON. It
uses the same transport envelope as live analysis: one `analysis_json` string
whose decoded value is the strict inner analysis object.

```bash
.venv/bin/python scripts/run_social_community_lab.py \
  --manifest examples/social_community_lab_targets.example.json \
  --acquisition-result path/to/acquisition.json \
  --analyst-response path/to/recorded-analysis.json \
  --allowed-tile-ids '["tile-community", "tile-voice"]' \
  --output out/social-community-lab/offline.json
```

No LLM or network call is made in this mode. The untrusted response goes
through the accepted C validator's one-shot boundary; there is no public retry
or second-attempt option. Citations must match admitted content IDs and role-derived constraints. The caller-owned
`--allowed-tile-ids` array is the only source of advisory candidate IDs. An
empty array means no candidates are permitted; the runner never reads an SV9
rubric to populate it.

Any already-validated analysis artifact crossing the composition edge is still
treated as untrusted input: the core reconstructs it from the supplied
observations and reruns the same strict citation, source-role, state, and tile
allowlist checks. The runner never JSON-copies a prebuilt artifact, and rejects
unknown scoring/state fields or invented citations, roles, and candidates.

The replayed acquisition matrix is reconstructed at the same boundary. For
Twitter/X, community-response and brand-reply/reply-tree capabilities always
remain `not_acquired` with reason `no_verified_reply_tree_endpoint`, even when
the replay claims they were acquired. Supported profile and official-post
capabilities are not suppressed by this interaction-specific guard.

The input response hash is bound into `run_identity`. Output paths are not
bound, so moving the same run to another private path does not change its run
identity.

### Optional live Gemini analysis

```bash
export SCRAPECREATORS_API_KEY='...'
export GEMINI_API_KEY='...'
.venv/bin/python scripts/run_social_community_lab.py \
  --manifest examples/social_community_lab_targets.example.json \
  --live-analysis \
  --allowed-tile-ids '["tile-community"]'
```

This mode is intentionally strict. The composition edge instantiates the
existing `LLMAnalyzer` only when its base URL is the Gemini API and its
`_call_json_gemini_native` method is available. It sets `use_cache = False`
before invoking anything, calls that native method exactly once, and keeps the
C analyzer at one attempt. It never falls back to `_call_json`, another
provider, or a hidden retry. If that path cannot be proven, the command fails
closed with a concise unavailable error; it does not weaken the file-only
boundary. The accepted acquisition is atomically checkpointed before the
Gemini call. If the call raises or its response fails domain validation, the
command exits non-zero **after** replacing that checkpoint with a final
fail-closed artifact: admitted observations, provenance, response metadata, and
credit accounting remain available, `community_analysis` is unavailable, and
an `analysis_failure` object records the safe failure status/reason. No analyst
claims or promotion readiness are fabricated.

The provider-facing Gemini schema is deliberately tiny: one required string
field named `analysis_json`. The system prompt describes the exact inner JSON
contract and caller-owned tile allowlist. The core decodes that string once,
rejects malformed, non-object, or duplicate-key JSON, then passes the decoded
object to the authoritative local post-validator. Non-empty strings, unique
citations/roles, identity checks, source-role rules, and tile constraints remain
local fail-closed invariants. Raw provider text is not persisted.

## Artifact shape and privacy

The artifact is written only under the repository's ignored directory
`out/social-community-lab/` (default file `run.json`). Absolute destinations,
relative traversal, sibling/canonical paths, and symlink escapes are rejected
before any parent is created. Accepted files use an atomic replace and mode
`0600`. Its product sections are:

| Section | Purpose |
| --- | --- |
| `run_identity` | normalized manifest fingerprint, bounded options, contract versions, and recorded response hash when present |
| `acquisition_matrix` | target/role acquisition status and provenance references |
| `role_coverage` | counts and acquisition status for official posts, community responses, brand replies, and unclassified observations |
| `observations_by_role` | semantic observations grouped by role, retaining provenance/citation fields but not metrics |
| `community_analysis` | strict C result, including status, sections, citations, and limitations |
| `social_tiles` | v2-only capture-bound analysis, ordered verdicts, and component call counts |
| `advisory_tile_candidates` | validated candidates constrained only by the caller allowlist |
| `metric_context` | engagement/follower context keyed by content ID, separate from semantic analysis |
| `promotion_evidence` | starts at `insufficient`; the runner cannot promote itself |
| `analysis_failure` | present only after a live analysis failure; fixed safe status/reason, attempt count, and `claims_available = false` |
| `limitations` | explicit partial/not-acquired and laboratory-boundary limitations |
| `canonical_invariance` | starts at `not_checked` for the independent E gate |

Safe metadata also records the runner version, mode, timestamp, acquisition
summary, and body-free provider response metadata. Raw provider payloads are
included only with `--include-raw`, remain redacted, and stay in the private
0600 artifact.

Social/community data can contain personal information, usernames, comments,
and inferred opinions. Before using a target list, confirm a lawful purpose,
data-minimization basis, provider terms, retention/deletion policy, access
controls, and any regional privacy requirements. Treat raw response mode as
high sensitivity even after redaction; do not commit artifacts or copy them to
shared logs. Provider calls and Instagram reply expansion can incur credits;
review the provider's current pricing and terms before running a broad target
set.

`promotion_evidence.gates` is a deterministic checklist of evidence slots for
the independent E gate. Every slot starts as `not_checked` with empty evidence
and a requirement; the runner ignores caller/provider attempts to mark a slot
passed. The separate `canonical_invariance` section also remains
`not_checked` until E proves it. The ordered slots are
`role_attribution_accuracy`, `representative_interaction_coverage`,
`citation_traceability`, `semantic_id_stability`, `metric_invariance`,
`community_claim_boundary`, `strategist_incremental_value`,
`negative_control_precision`, `cost_latency_rate_limits`,
`privacy_retention_terms`, and `canonical_invariance`.

## Verification

The focused, offline suite is network-free:

```bash
PYTHONPATH=. .venv/bin/python -m pytest -q \
  tests/test_social_community_lab_cli.py \
  tests/test_social_community_lab.py \
  tests/test_scrapecreators_spike.py \
  tests/test_social_lab_contracts.py

.venv/bin/ruff check \
  scripts/run_social_community_lab.py \
  tests/test_social_community_lab_cli.py
```

The tests use temporary output paths, synthetic/replayed acquisition data, and
monkeypatched providers where needed. They do not use live network/provider
credits, default databases, or repository output artifacts.
