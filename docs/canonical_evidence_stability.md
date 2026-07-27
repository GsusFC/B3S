# Canonical evidence stability

## Verdict

The latest scan is not automatically the best scan. Acquisition providers and LLM evaluation are observations with failure modes, not authorities. B3S therefore keeps immutable scans and derives a separate temporal selection.

## Two independent axes

Each new scan is compared with:

1. the selected baseline, to decide whether the observed brand evidence changed; and
2. the immediately previous scan, to identify a local acquisition failure or evaluation drift.

The evidence fingerprint is deterministic and ignores generated evidence refs, record order, URL tracking parameters, casing, and whitespace. It retains source class, normalized URL, content hash, external-source clusters, acquisition state, and component-to-evidence links.

LLM interpretations and tile results have separate fingerprints. Temperature `0` reduces sampling variation but does not guarantee identical provider output, so score equality is never used as proof that acquisition was stable.

## Classifications

- `provisional`: first non-invalid baseline when the run is not fully reliable/pass.
- `canonical`: reliable/pass baseline or a reliable stable repeat of a provisional baseline.
- `stable`: evidence and evaluation agree with the baseline.
- `evaluation_drift`: materially equivalent evidence produced different interpretation or tile results.
- `acquisition_regression`: previously observed owned, external, independent, or visual evidence was not reacquired, or the acquisition gate worsened.
- `candidate`: material evidence changed and needs confirmation rather than silently replacing the baseline.
- `invalid`: broken run or a component that could not be evaluated.

All scans remain accessible in the history. Canonical status is a derived projection and never rewrites a stored report.

## Publication modes

`B3S_CANONICAL_ENFORCEMENT_MODE` accepts:

- `observe`: compute and expose classifications, but keep the newest scan visible.
- `repeated`: enforce the selected canonical/provisional report only for histories with two or more scans. A single scan behaves exactly as before.
- `all`: enforce the selection for every history.

Production configuration starts at `repeated`. Rollback is a configuration-only change to `observe`; no reports or comparisons need deletion.

## Global dry-run

Run:

```bash
.venv/bin/python scripts/canonical_evidence_dry_run.py --format markdown
```

Optional arguments:

```text
--reports-dir PATH
--format json|markdown
--output PATH
```

The command reads every available history and writes nothing unless `--output` is explicitly supplied. It reports:

- repeated versus single-scan brands;
- classification counts;
- latest and selected report IDs and scores;
- brands whose visible report would change under `repeated` or `all`.

The first production dry-run and its Ticketeame classification are recorded in
[`canonical_evidence_production_dry_run_2026-07-27.md`](canonical_evidence_production_dry_run_2026-07-27.md).

## Persistence and API

PostgreSQL migration `002_evidence_stability.sql` stores:

- deterministic capture fingerprints;
- comparisons against baseline and previous evaluation;
- the current canonical/provisional selection.

The original `report_snapshots.payload` remains byte-for-byte immutable. The brand history API adds current reliability, classification, reason codes, and selected report IDs. Scan result payloads include the scan-time stability assessment.

## Guardrails

- A comparison failure marks the new scan `non_canonical`; it does not block storage or erase the report.
- A structurally valid tile profile survives a later corrective LLM retry that returns invalid JSON.
- LLM cache keys include effective temperature, JSON schema, and strictness, preventing incompatible calls from sharing cached output.
- Missing evidence is treated as unknown acquisition loss, never as verified brand deterioration.

## Current limitation

`candidate` changes are quarantined and require an explicit promotion policy. The initial rollout deliberately optimizes against false deterioration: it can keep an old baseline longer than necessary, but it cannot silently publish a lower score caused only by Exa or LLM degradation.
