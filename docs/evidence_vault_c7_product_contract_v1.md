# Evidence Vault C7 product contract v1

Status: active.

## Decision

C7 (`Web-redes`) is a normal, fully functional Coherencia tile. It uses the
same product lifecycle as the other rubric tiles:

- capture and incremental refresh;
- candidate construction and human review;
- adoption into canonical Vault memory;
- whole-group reopening when its reviewed members materially change;
- normal Coherencia scoring;
- report, UI, Scanner API, and history visibility.

C7 keeps its tile-specific evidence rule: an accepted result must be backed by
one reviewed `all_of` group containing the owned-web and external-social channel
members. This is an evidence-integrity rule, not an operational gate.

## Non-blocking invariant

C7 never has independent authority to block a scan, capture persistence,
operation-plan claim or execution, report publication, API/UI availability,
process startup, deployment, or rollback. Missing, stale, contradictory, or
unreviewed C7 evidence changes only C7's ordinary lifecycle state and the
resulting Coherencia score.

There is no C7-specific master switch, emergency deny, domain allowlist,
cutover endpoint, or runtime snapshot requirement. Generic environment and
Vault pipeline controls continue to govern the whole deployment, not C7 alone.

## Provenance diagnostics

The verified-raw C7 shadow evaluator and immutable migrations remain available
as private provenance diagnostics. Their result can explain whether the exact
cross-channel evidence satisfies the strongest provenance profile. It does not
authorize or deny C7 functionality and must not be used as a deployment,
publication, or availability gate.

## Migration boundary

Existing migrations and persisted versioned artifacts are immutable. Forward
migration 024 projects only exactly mapped active relation fingerprints into
incremental planning; missing or ambiguous lineage is omitted rather than made a
scan/persistence gate. Historical fields whose names mention cutover remain
historical data-contract fields; they do not recreate a runtime control plane.
