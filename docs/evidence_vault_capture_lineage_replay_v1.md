# Evidence Vault capture lineage and report replay v1

Status: implemented on the stacked **Draft — DO NOT MERGE** lineage branch. This
contract is Vault-only, has no production or scanner runtime effect, and is not
deployment or cutover authorization.

## Truth boundary

A full historical report is not the original acquisition envelope. Its
`raw.flow.candidate` is a post-flow candidate snapshot. Replay therefore labels
it `report_derived_candidate_capture` and records its append origin as
`report_derived_candidate_capture_replay`. That provenance can never satisfy
runtime raw-lineage readiness.

The pure replay adapter accepts a caller-supplied report dictionary plus its
raw-file SHA-256. Because it does not receive the source bytes, that digest is
stored as `declared_unverified_no_source_bytes`; it is not represented as a
verified byte measurement. The adapter performs no network, provider or LLM
calls.

## Logical seed export

`evidence-vault-lineage-seed-export-v2` is content-addressed. Validation rebuilds
the capture observation, normalized evidence pack, exact evidence identities,
C7 `all_of` group and manifest fingerprints. Exact quotations, references,
URLs, roles and frozen identities must match. Credential-shaped keys, unknown
schemas, malformed hashes and authority/effect claims fail closed.

The logical seed proves deterministic content integrity only. It does not prove
operator identity, authenticity, raw acquisition provenance, accepted authority
or physical backup completeness. `pg_dump -Fc` and `pg_restore` remain the
physical backup and recovery mechanism.

## Durable capture-observation journal

Migration `017_evidence_vault_capture_lineage.sql` adds three append-only
relations:

- a per-brand journal for new `persist_capture_observation()` commits;
- exact operational-source/report-derived capture checkpoints; and
- exact checkpoint members bound to durable evidence records.

This is not a complete journal of the legacy `captures` table. `import_report()`
and pre-017 captures are intentionally not backfilled, including legacy imports
performed after migration 017. Legacy immutable reports remain compatible and
unmodified.

A new capture observation and its journal event commit in one transaction while
holding the existing brand advisory lock. Sequence order is database commit
order, not caller-controlled `observed_at`, `recorded_at`, scan ID or UUID order.
An exact retry returns the existing row and does not advance the sequence. The
neutral `capture_observation_commit` origin means only that the typed capture
observation path persisted the row; it is not a live/raw acquisition
attestation.

Database insert triggers enforce watermark predecessor adjacency and enforce
that a first report checkpoint begins at its actual capture sequence. Later
checkpoints for the same exact source must be at the next sequence and preserve
that origin. Database triggers also reject every update and delete. Stored rows
retain `authority=false`, `production_runtime_effect=false` and
`scanner_runtime_effect=false`.

A stale exact retry may recover an already-existing checkpoint. It cannot create
a new stale checkpoint, skip a capture sequence, or claim an earlier origin.

## Runtime readiness

Accepted group authority and present-time readiness are separate APIs.
`get_evidence_vault_active_c7_group_attestation()` preserves historical accepted
authority. `get_evidence_vault_runtime_ready_c7_group_attestation()` currently
returns no attestation by construction: migration 017 admits only
report-derived audit bindings, and this Draft contains no trusted live or
verified-raw artifact variant.

The cutover adapter calls the runtime-readiness API before operational score
get-or-create and again before projection construction. It also rereads the kill
switch and allowlist at every effect boundary. Consequently this branch cannot
present operational C7 even if its Vault flags are changed. A later change must
introduce a separately validated, trusted acquisition provenance before the
runtime API may return authority.

## External recovery observation

Outside Git, five canonical full Causa Prima reports were recovered from the
PostgreSQL 18 backup. This is an external recovery observation, not a fixture or
claim independently reproducible from this diff. The first two rederive both
frozen C7 members. The last three retain the owned-web member but do not
reacquire the LinkedIn member. That condition is treated as latest coverage
loss, not contradiction or refutation. The reports and generated packets remain
outside Git because they contain sensitive evidence.

Those reports can close deterministic report-derived audit lineage. They cannot
close original raw acquisition provenance. Causa Prima therefore remains
runtime-not-ready and must not enter the allowlist.

## Remaining NO-GO conditions

- the original accepted raw acquisition envelopes are unavailable;
- there is no trusted builder or validator for live/verified-raw lineage;
- legacy parentless genesis/seed remains unresolved and preview or score output
  is not a valid v2 parent;
- there is no single transactional readiness token spanning current memory,
  capture checkpoint, score evaluation and presentation;
- the offline history adapters still live in `src/history/repository.py`;
- production scanner wiring for `prepare_vault_scan_after_capture()` remains
  absent;
- no backup, rollback or deployment authorization has been granted for this
  Draft branch.

Do not enable the allowlist, deploy Vault, mark the PR ready, merge, auto-merge
or change production based on this implementation.
