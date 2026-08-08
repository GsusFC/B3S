# PR #70 — C7 Verified Raw/Live Provenance Contract

Status: implementation contract for the Draft PR #70 stack based on PR #69 head `95e3591`. Production remains `NO-GO`.

## 1. Verdict

The current history chain is content-addressed and immutable, but it proves only **what B3S persisted**, not **that the two C7 source observations were produced by a trusted live acquisition**. Production must remain `NO-GO`.

Evidence:

- the normal scanner creates a real pre-analysis snapshot in memory, then feeds it to analysis and saves only the finished report; it never calls the existing Vault capture-persistence seam (`web/scan_runner.py:241-299,718-805`).
- `parse_capture_observation()` hashes caller-supplied JSON and normalized evidence, but the acquisition envelope has no independent authenticity proof (`src/history/capture_observation.py:41-133`).
- the currently unused live seam could freeze the full `snapshot`, derived evidence, attempts and generic artifacts, all with `authority=false` (`src/services/evidence_vault_scan_orchestration.py:541-623`; persistence mapping at `src/history/repository.py:7984-8180`).
- the web collector emits `web-capture-provenance-v1` with URL, provider, timestamp and a markdown hash, but no keyed attestation and no evidence-span binding (`src/collectors/web_collector.py:698-720`).
- evidence normalization truncates source material and assigns roles/classes after acquisition (`src/sv9_flow/evidence_worker.py:89-125`, `658-868`).
- C7 role assignment accepts an exact owned host plus a LinkedIn `/company/` external row, but does not itself prove a live fetch or cross-channel ownership link (`src/services/evidence_vault_exact_relation_supplement.py:1313-1326`).
- migration 017 deliberately admits only `report_derived_candidate_capture` (`src/history/migrations/017_evidence_vault_capture_lineage.sql:77-139`).
- historical replay explicitly says its byte hash is declared, not reverified, and that it is not an original raw acquisition envelope (`src/services/evidence_vault_lineage_replay.py:180-300`).
- both repository runtime-readiness methods deliberately return `None` (`src/history/repository.py:5836-5863`).

## 2. Threat model

PR #70 must fail closed against:

1. a caller or direct SQL writer fabricating URLs, timestamps, provider labels or hashes;
2. copying one valid receipt to a different scan, capture, brand or evidence row;
3. report-derived replay being upgraded to live provenance;
4. evidence normalization that is not reproducibly tied to the acquired source fragment;
5. redirects or canonical URLs that change the owned brand identity;
6. an unrelated LinkedIn company page being attributed to the brand;
7. zero, one, three, duplicated or cross-capture C7 members;
8. stale receipts, future timestamps, mixed scans and newer contradictory captures;
9. replay divergence, concurrent watermark writes, reparenting, mutation, deletion or `TRUNCATE`;
10. missing/malformed verification keys, policy versions or runtime controls.

Database superuser compromise is outside the achievable application threat boundary. A normal application-role SQL writer must not be able to manufacture readiness without the acquisition private signing key.

## 3. New acquisition contract

### 3.1 `evidence-vault-raw-acquisition-receipt-v1`

After all transport adapters return but before interpretation, the scan worker freezes a `pre_receipt_snapshot` and computes its canonical SHA-256. Every qualifying source observation then carries one canonical receipt with three exact layers:

1. `claims` (exact fields only, `extra=forbid`):
   - schema/policy version, `key_id`, globally unique 128-bit `receipt_nonce` and `acquisition_session_id`;
   - exact `workspace_slug`, `source_scan_id`, canonical brand and channel role;
   - `pre_receipt_snapshot_sha256` plus provider/acquisition mode;
   - acquisition-mode union: direct HTTP/browser requires requested URL, complete bounded redirect chain and final URL; provider API requires exact provider-request fingerprint, result ordinal and reported source URL with an empty redirect chain;
   - fetched time, transport/provider status, selected non-secret headers, media type and byte count;
   - strict JSON pointer to the source fragment in the persisted capture payload;
   - raw-fragment and extracted-document SHA-256 plus extractor version;
   - external-identity provenance fingerprint for the external role.
2. `receipt_fingerprint = canonical_fingerprint('evidence-vault-raw-acquisition-receipt-v1', claims)`.
3. `signed_payload = {signature_schema='evidence-vault-ed25519-signature-v1', claims, receipt_fingerprint}` and `signature = Ed25519.sign(private_key[key_id], canonical_json(signed_payload))`, with strict base64 and length validation. The stored receipt adds only `signature`; no fingerprint/signature input is circular.

The final capture observation includes the frozen raw snapshot, exact receipts and an order-independent `receipt_set_fingerprint`; its observation/capture hashes are recomputed at persistence. Migration 019 makes `(key_id, receipt_nonce)` and `receipt_fingerprint` schema-global unique and binds the receipt set to the exact workspace/scan/capture FK chain, preventing cross-workspace or cross-capture transplant.

PR #70 adds an explicit `cryptography` dependency. The acquisition signer alone receives the private key; PostgreSQL and API/runtime receive only a strict bounded public-key registry. Missing, unknown, revoked or malformed keys mean no verified receipt. Rotation accepts explicitly versioned public keys; new signing uses exactly one current key.

There is no signer inside the FastAPI process. PR #70 extracts acquisition into a separate worker/process identity that alone holds the private key and scanner-ingest credential. Its narrow command accepts only `{workspace_slug, source_scan_id, canonical brand/url}`—never caller-supplied raw payloads or receipt claims—performs the transport collection itself, freezes/signs/persists the snapshot atomically, and only then returns the signed public result. API/report/replay/runtime receive public verification keys only and have no constructor or IPC verb that signs arbitrary content.

### 3.2 Qualifying roles

**`owned_web`**

- provider is one of `firecrawl`, `direct_http`, `browser_fallback`;
- status is 2xx and extracted content is non-empty;
- requested host and final host canonicalize to the exact brand domain (only the existing explicit `www` equivalence is allowed);
- redirects containing credentials, ports, IP literals, non-HTTP(S), Unicode/punycode ambiguity or a different registrable identity reject the receipt.

**`external_social_profile`**

- provider is `exa` in v1; SearchAPI and generic snippets remain ineligible until they emit the same contract;
- the provider-reported source URL is strict HTTPS LinkedIn `www.linkedin.com/company/<non-empty-slug>` with no credentials/port;
- the persisted `external-identity-provenance-v1` reproduces strongly and requires no human-review fallback;
- brand association is proved by at least one signed raw fact: the owned capture links to the exact LinkedIn company URL, or the external payload exposes a canonical website whose domain exactly matches the brand. Name similarity alone is insufficient.

### 3.3 Time policy

Version `evidence-vault-c7-live-freshness-policy-v1`:

- `received_at` is generated by PostgreSQL and cannot be caller overridden;
- signed `fetched_at` may be at most 5 minutes ahead of database time and must be within 15 minutes of `received_at`;
- both members belong to the same workspace, acquisition session, `source_scan_id` and capture;
- member timestamps differ by no more than 15 minutes;
- eligibility expires at `LEAST(older_fetched_at, database_received_at) + 24 hours`, evaluated against database time;
- a newer brand watermark immediately makes the prior live attestation ineligible.

These constants align the first contract with the existing 24-hour raw-input cache boundary (`src/storage/raw_inputs.py:61-63`) and are policy-versioned so later changes cannot reinterpret old rows.

### 3.4 Deterministic evidence binding

A qualifying evidence row must bind to one signed receipt through:

- exact capture and brand foreign keys;
- extractor schema/version;
- persisted extracted document (bounded text/bytes) and its SHA-256;
- byte offsets or a strict JSON pointer selecting the evidence passage;
- passage SHA-256 and evidence record content hash;
- exact URL and source role.

The repository re-resolves the raw-fragment pointer from `captures.raw_payload`, recomputes all hashes, reproduces extraction, and verifies the Ed25519 signature before inserting any provenance row. The C7 quote must remain an exact non-empty substring of the bound evidence content.

## 4. Migration `019_evidence_vault_verified_raw_provenance.sql`

Do not edit migrations 017 or 018.

Create append-only tables:

1. `evidence_vault_raw_acquisition_receipts`
   - one content-addressed signed receipt per exact workspace/scan/capture binding;
   - exact schema/policy/role/provider checks;
   - schema-global unique `(key_id, receipt_nonce)` and receipt fingerprint;
   - capture/brand/watermark linkage.
2. `evidence_vault_raw_evidence_bindings`
   - exact receipt-to-evidence/extraction binding;
   - same brand/capture enforced by composite foreign keys;
   - immutable binding fingerprint.
3. `evidence_vault_verified_c7_lineage_bindings`
   - binds one exact operational source packet/group to the current capture watermark and freshness policy;
   - never accepts the migration-017 report-derived binding as proof.
4. `evidence_vault_verified_c7_lineage_members`
   - exactly two members, one per role, distinct receipt/evidence/source identity;
   - both from the same capture and verified group.
5. `evidence_vault_raw_provenance_disposition_events`
   - targets one exact receipt set/binding and chains a per-target sequence, predecessor/event fingerprint and idempotency key;
   - DB-enforced actions `retain|place_legal_hold|release_legal_hold|revoke_runtime`; initial state is retained, hold/release must alternate, and runtime revocation is terminal;
   - revocation immediately denies later snapshots but does not erase history. PR #70 provides no physical-purge or crypto-shred claim: the frozen plaintext snapshot remains in immutable PostgreSQL parents. Any future erasure requirement needs a separately reviewed encrypted external-reference storage design.

Before creating the journals, migration 019 performs a fail-closed preflight and adds the missing composite parent constraints: scan workspace+brand must reference one brand; capture brand+scan must reference one scan; operation-plan workspace+brand+scan must reference the same parents. Existing conflicting data blocks migration instead of being silently reparented.

SQL requirements:

- canonical JSON uses `ORDER BY key COLLATE "C"`;
- every schema and hash comparison is NULL-safe (`IS DISTINCT FROM`);
- database triggers recompute all non-secret fingerprints;
- deferred constraint triggers validate complete two-member groups at commit;
- deferred identity validation ties workspace/brand/scan/capture, packet and manifest identity, exact artifact brand/subject, owned final origin and operational plan to the same canonical brand;
- parent identity is universally immutable; all new write paths preserve the existing global order `source_scan/idempotency advisory lock -> canonical brand advisory lock -> dependent rows`;
- row UPDATE triggers never wait for an advisory lock;
- reject UPDATE/DELETE and statement-level `TRUNCATE` on all five named journals;
- migration preflight validates composite-parent integrity globally, but exact typed-observation/evidence-set checks apply only to 017-watermarked typed observations and new 019 envelopes—legacy/report rows are not backfilled or reclassified;
- deferred validation recomputes observation/capture/evidence hashes from eligible durable payloads and proves the exact evidence set—no missing or extra evidence rows;
- an existing scan/capture without its required watermark is a hard conflict, never a healthy `unchanged` replay;
- new journals are owned by a `NOLOGIN` object-owner role. A separate tightly controlled `LOGIN` migrator DSN may `SET ROLE` to that owner only in the release migration job and is absent from runtime. Runtime gets only the read function, a distinct scanner-ingest role gets one ingest function, and a separate provenance-governance operator role gets one disposition-append function. Both writes are `SECURITY DEFINER` with fixed safe `search_path`, fully qualified objects, no dynamic SQL, and `REVOKE EXECUTE FROM PUBLIC`; none of those roles gets table DML or trigger-disable privileges. Production startup verifies the migration head but cannot apply DDL;
- the scanner-ingest credential and private key exist only in the separate trusted acquisition worker process; the FastAPI/report runtime environment contains neither. SQL cannot grant readiness: Python re-verifies Ed25519 at ingestion and every shadow/readiness read; a structurally inserted but invalid receipt is permanently non-qualifying;
- no `authority=true`, production effect or scanner effect column is introduced.

Ed25519 verification remains mandatory in Python at ingestion and at every readiness read; SQL structural validation alone never grants readiness.

## 5. Repository/service API

Add pure strict validators/builders plus repository methods:

- `TrustedAcquisitionWorker.capture(command)` — separate process/service; collects, freezes, signs and atomically persists before returning;
- `verify_raw_acquisition_receipt()` — strict Ed25519 verification against the public-key registry;
- `persist_capture_observation_with_receipts()` — worker-only parse/verify/atomic capture, evidence, receipt, extraction, watermark ingestion through the narrow DB surface;
- `web/scan_runner._run` becomes a client of that narrow acquisition command and receives an already persisted signed snapshot before `build_flow_sv9_shadow_eval`; it never receives the private key or ingest DSN. A worker/persistence failure is explicit and report import can never substitute for live provenance;
- `bind_evidence_vault_verified_c7_lineage()` — rederive exactly two members from durable rows and the exact stored source packet;
- `get_evidence_vault_c7_shadow_readiness()` — transactional diagnostic result with stable reason codes; no runtime effect.

Replay rule: deterministic IDs and `INSERT ... ON CONFLICT DO NOTHING`, followed by a complete stored-content comparison. Exact replay succeeds; any divergence raises a conflict. A retry never regenerates `fetched_at`, nonce or signature.

PR #70 keeps both `get_evidence_vault_c7_runtime_snapshot()` and `get_evidence_vault_runtime_ready_c7_group_attestation()` returning `None`. It adds shadow proof, not cutover.

## 6. Shadow-readiness predicate

A brand is `verified_raw_ready` only when one repeatable-read transaction proves all predicates:

1. provenance public-key, freshness and retention configuration is strictly valid; shadow proof does not require `current_c7_cutover_decision.enabled`, an allowlist or production flags, and both production runtime stubs remain `None`;
2. current operational memory has exactly one accepted C7 and no C7 pending reassessment;
3. the accepted group is the existing exact two-member `all_of` group;
4. reviewed packet, exact source packet, accepted decisions and current memory all bind exactly as PR #69 requires;
5. one new verified-raw binding targets that exact packet/group;
6. its watermark is the current brand head;
7. it has exactly two distinct members and roles `{owned_web, external_social_profile}`;
8. every receipt signature, raw locator, raw/extracted/passage hash and evidence binding revalidates;
9. both receipts satisfy the v1 time policy and deterministic brand association;
10. the current policy/key/group/attestation fingerprints match;
11. no report-derived provenance row participates;
12. the score evaluation is already persisted and exactly matches the same current memory, including `adoption_event_id`;
13. the latest disposition chain for the exact receipt set/binding is valid and has no `revoke_runtime` event inside the same transaction;
14. the returned shadow snapshot has one strict schema version and a recomputed snapshot fingerprint over memory, evaluation, provenance, disposition and attestation identities—never independently trusted mappings.

Any missing, duplicated, malformed, stale or changing input returns a stable fail-closed reason and no attestation. PR #70 shadow results are explicitly “as of this repeatable-read snapshot”; a later runtime cutover must fence capture/adoption/disposition writes with the same brand lock or recheck a final head+revocation token before presentation.

## 7. Required tests

### Pure tests

- exact-field/schema validation and canonical fingerprints;
- valid Ed25519 signature, tamper of every signed field, unknown/revoked key, malformed public-key map and rotation;
- process-boundary test: web/report environment cannot import/invoke arbitrary signing or access private-key/ingest credentials; worker IPC rejects caller-supplied raw/claims;
- URL/redirect/LinkedIn path normalization, IP/credential/port/punycode rejection;
- raw JSON pointer, extractor, offsets, passage and evidence hash reproduction;
- external identity and owned-link association;
- future, skewed, mixed-scan and expired receipts.

### PostgreSQL 14/16/18

- exact replay and divergent replay;
- runtime-role denial for DDL, `SET ROLE`, journal DML and trigger disable; release migrator alone can authenticate, assume the `NOLOGIN` owner and apply/validate 019; forged schemas/hashes/signatures cannot become shadow-ready;
- workspace/brand/scan/capture/operation-plan composite-parent contamination;
- wrong observation/capture/raw/evidence hash, missing/extra evidence and unwatermarked existing-capture replay;
- zero/one/three members, duplicate roles, duplicate receipts, cross-brand and cross-capture bindings;
- report-derived capture/binding cannot qualify;
- evidence/receipt/binding reparenting and parent mutation;
- UPDATE, DELETE and `TRUNCATE` rejection; legal-hold/release/runtime-revocation transition and operator-authorization behavior;
- concurrent capture/receipt/binding writes under one brand lock;
- regression against row-lock/advisory-lock inversion;
- newer watermark invalidates prior readiness;
- concurrent memory adoption/capture yields one atomic old/new/None shadow snapshot, never a mixed state;
- canonical JSON parity under non-C database collation.

### Integration

- separate acquisition worker persists the exact pre-analysis snapshot once before interpretation; FastAPI process has no private key/ingest credential, failure is explicit, and report import cannot substitute;
- real owned-web + Exa LinkedIn capture creates two signed receipts and one shadow-ready C7 group;
- blocked/partial providers stay non-ready without manufacturing evidence;
- crash after capture, after receipts and after binding replays idempotently;
- public brand GET remains write-free and operational C7 remains absent;
- authenticated API remains private/no-store and returns unavailable while runtime snapshot is disabled.

## 8. PR #70 scope and sequence

Branch from exact PR #69 head; target PR #69, not PR #68 or `main`.

1. pure receipt/signature/extraction contract;
2. collector emission for owned web and Exa LinkedIn only;
3. migration 019, composite-parent preflight/constraints, privileges and atomic persistence;
4. extract live collection/signing/persistence from the FastAPI thread into a separate worker identity; make `web/scan_runner` a narrow client, and keep report import separate/non-qualifying;
5. verified C7 binding, retention/revocation events and shadow-readiness evaluator;
6. correct stale cutover/lineage docs to describe the no-write seam and quarantined template;
7. negative/concurrency/PG14-16-18 suites;
8. Draft PR with production explicitly `NO-GO`.

Do not include: physical purge/crypto-shred claims, legacy scorer changes, public C7 presentation, runtime snapshot enablement, backfill of report-derived rows, broad provider expansion, deployment, key provisioning, PR #69 edits or migration 017/018 edits.

## 9. Exact exit criteria from `NO-GO`

A later cutover PR may be proposed only after all are true:

### Code gate

- PR #70 is independently reviewed and merged through its stack;
- full suite and the complete provenance matrix pass on PostgreSQL 14, 16 and 18;
- no unresolved P0/P1 security, SQL, replay, race or trust-boundary findings;
- acquisition collection/signing/persistence runs in a separate worker process/service identity; the FastAPI/report runtime demonstrably has no private key or ingest credential; strict public-key configuration is available and missing/malformed/revoked configuration fails closed;
- retention duration, legal-hold ownership and logical runtime-revocation policy for the immutable plaintext snapshot are explicitly approved;
- atomic runtime snapshot implementation is separately reviewed; serving a request performs no writes.

### Live-data gate, per initially allowlisted brand

- two consecutive post-contract real captures—not report replays, at least 5 minutes apart and no more than 24 hours apart—each contain two valid signed receipts from one scan; the most recent is the current head;
- raw fragments remain retrievable and reproduce receipt, extraction, passage and evidence hashes;
- owned domain, exact LinkedIn company URL and deterministic cross-channel association validate;
- exact two-member group is human-reviewed, adopted, current and has no pending reassessment;
- verified binding targets the current watermark and remains inside the 24-hour policy;
- persisted score evaluation matches the same canonical memory;
- shadow readiness succeeds for both consecutive captures after any required human review, with no bypass, manual database repair or reuse of a prior receipt nonce.

### Operational gate

- emergency deny has been exercised successfully;
- cutover starts with one explicit domain allowlisted, master disabled by default and rollback documented;
- monitoring exposes readiness failures, stale provenance and signature/key failures without leaking receipt contents;
- a named human operator approves the live evidence and cutover window.

Until every code, live-data and operational item is true, production remains `NO-GO`; accepted historical C7 memory may exist, but it has zero runtime/scanner effect.
