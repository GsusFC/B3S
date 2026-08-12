# Evidence Vault capture lineage and report replay v1

Status: implemented on the stacked **Draft — DO NOT MERGE** lineage branch. This
contract is Vault-only, has no production or scanner runtime effect, and is not
deployment or cutover authorization.

## Truth boundary

A full historical report is not the original acquisition envelope. Its
`raw.flow.candidate` is a post-flow candidate snapshot. Strictly validated audit bindings therefore label it
`report_derived_candidate_capture`. The generic capture journal remains neutral
and carries no export identity or provenance claim. Report-derived binding
provenance can never satisfy runtime raw-lineage readiness.

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

## Provenance diagnostic boundary

`get_evidence_vault_active_c7_group_attestation()` can rederive the exact
historical group authority. Report-derived replay still cannot claim verified
raw acquisition provenance. That distinction affects the private provenance
diagnostic only; it does not activate or deny the functional C7 tile.

The former runtime-readiness stubs, kill switch, allowlist, and separate C7
presentation adapter were retired. C7 now follows the ordinary tile lifecycle
described in
[`evidence_vault_c7_product_contract_v1.md`](evidence_vault_c7_product_contract_v1.md).

## External recovery observation

Outside Git, five canonical full Causa Prima reports were recovered from the
PostgreSQL 18 backup. This is an external recovery observation, not a fixture or
claim independently reproducible from this diff. The first two rederive both
frozen C7 members. The last three retain the owned-web member but do not
reacquire the LinkedIn member. That condition is treated as latest coverage
loss, not contradiction or refutation. The reports and generated packets remain
outside Git because they contain sensitive evidence.

Those reports can close deterministic report-derived audit lineage. They cannot
close original raw acquisition provenance. That limitation remains visible in
the optional diagnostic but does not suppress C7's normal product lifecycle.

## Historical scope boundary

This document records constraints of the former Draft lineage branch. It grants
no deployment authorization, and it no longer defines a C7 readiness or
allowlist gate. Current C7 behavior is defined by
[`evidence_vault_c7_product_contract_v1.md`](evidence_vault_c7_product_contract_v1.md).
