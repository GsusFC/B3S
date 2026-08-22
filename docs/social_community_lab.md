# Social/community laboratory runner

`run_social_community_lab.py` is a private laboratory composition edge. It
joins the accepted target manifest and ScrapeCreators acquisition contract to
the citation-bound social analysis contract, then writes one atomic
`b3s-social-community-lab-v1` artifact.

The lab is deliberately **not** a Scanner, Evidence Vault, or SV9 path. It
does not calculate a score, activate a tile, write a canonical state, write
SQLite/PostgreSQL, or promote evidence. Promotion starts as `insufficient` and
canonical invariance starts as `not_checked` for an independent E gate to
replace or prove.

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
recorded analyst response may be a JSON file path, `@path`, or inline JSON:

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
boundary.

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
| `advisory_tile_candidates` | validated candidates constrained only by the caller allowlist |
| `metric_context` | engagement/follower context keyed by content ID, separate from semantic analysis |
| `promotion_evidence` | starts at `insufficient`; the runner cannot promote itself |
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
