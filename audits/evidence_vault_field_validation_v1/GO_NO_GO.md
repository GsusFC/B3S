# Evidence Vault v2 — Real-brand pre-cutover gate

## Verdict

**Production cutover: NO-GO.**

**Continued Vault-only shadow and human review: GO.**

The core is now exercised against a frozen two-brand corpus and a disposable
PostgreSQL executor. Scanner/model output remained non-authoritative. An
identified human subsequently resolved all 19 relations, producing
Vault-authorized memory and scores only inside the disposable database. There
is still no live scanner wiring or production presentation effect.

## Scope and truth boundary

The frozen corpus is `normalized_evidence_pack_only`. It preserves normalized
historical evidence packs, not original acquisition envelopes. Therefore this
gate validates fixture integrity, planning, incremental scoping, executor
bounds, immutable persistence, and non-authority. It does **not** validate raw
acquisition replay.

The Causa Prima downstream reference is derived legacy output. Its null
`reviewer_id` / `event_id` rows are regression context, not durable Vault v2
human decisions, and are not importable as authority.

The original field implementation is preserved byte-for-byte at recovery commit
`f9d22c747793447ef18a6855e96f8747f05b2b77`. This clean landing branch retains
the validated implementation, excludes the incidental untracked lockfile, and
regenerates the content-addressed implementation map against the committed
landing tree. The field artifacts and disposable database checkpoints remain
bound to the preserved recovery snapshot rather than being presented as a new
field execution. Their durable paths are local preservation references, not
Git-distributed fixtures; a fresh clone can verify the tracked audit bundle but
cannot independently restore the exact field databases without the separately
preserved checkpoints.

The normalized real-brand fixtures intentionally retain the exact audited
public-source text and acquisition metadata, including two workstation-local
source paths. The absent raw source envelopes and their SHA-256 references are
historical provenance attestations, not independently reconstructible inputs.
This corpus therefore remains a private validation artifact and is not a
cutover or redistribution authorization.

## Offline deterministic gate

| Case | Baseline context | Baseline classified | Transition | Classified | Expected impact |
|---|---:|---:|---|---:|---|
| SoccerSolver | 137 | 130 | identical | 0 | `none` |
| SoccerSolver | — | — | later material delta | 10 | `review_required` |
| Causa Prima | 47 | 42 | later material delta | 7 | `review_required` |

All identical observation replays produced zero-LLM plans. Every plan retained
`authority=false`, `runtime_effect=false`, no canonical score recalculation,
and no report creation.

The frozen Causa Prima downstream reference also retained:

- chronology: `a8ba05137817 → 5bbeaa6dc58f → 76b281740607 → c08a4847f063 → 5d713fcfb53a`;
- 137 accepted identities and 250 occurrences;
- 80 tile rows;
- reproducible current score `91 → 91` and non-authoritative preview `93`;
- 30 legacy identity decisions, explicitly rejected as Vault v2 authority.

## Semantic PostgreSQL shadow

Model: `gemini-3.1-flash-lite`. Provider-generated outputs were observed during
this validation session; retries/final replay reused the local artifact cache.

| Case | Selected | Semantic | Shortlisted pairs | Relations retained | Model rows rejected | Shortlist truncations |
|---|---:|---:|---:|---:|---:|---:|
| SoccerSolver | 130 | 130 | 135 | 8 | 1 | 0 |
| Causa Prima | 42 | 42 | 244 | 11 | 0 | 1 evidence / 6 pairs |

The SoccerSolver rejection was fail-closed: the model submitted a tile outside
the evidence-specific shortlist. Only its canonical hash and rejection reason
were persisted. The raw invalid relation did not enter the source packet.

The Causa Prima truncation was deterministic and visible: one evidence row had
30 eligible tiles, the executor retained the bounded first 24 and recorded the
six omitted tiles (`PR4`–`PR9`).

Both operations completed with `attempt_count=1`. Re-running the harness kept
the same immutable result fingerprints and attempt counts, so completed
operations did not reclaim work or repeat semantic execution.

For both cases the harness asserted:

- source and operational results remained `authority=false`;
- `has_accepted_change=false`;
- no operational memory head existed;
- no score evaluation was created;
- no live scanner or production runtime effect occurred.

## Human review, adoption, and score

Reviewer `gsus` resolved all 19 rows at
`2026-08-07T09:59:45+02:00`: 14 accepted and 5 rejected. Immutable worksheet
columns matched the frozen semantic artifact exactly. The review time is stored
on the append-only relation events.

| Case | Accepted / rejected relations | Accepted tiles | Vault score | Completeness |
|---|---:|---:|---:|---|
| SoccerSolver | 7 / 1 | 6 | 6 | partial, 74 unresolved |
| Causa Prima | 7 / 4 | 4 | 4 | partial, 76 unresolved |

Accepted SoccerSolver tiles are `A1`, `I4`, `M1`, `P1`, `P3`, and `PE1`.
Accepted Causa Prima tiles are `P1`, `P3`, `P5`, and `PR3`. Every rejected
relation is absent from accepted memory. A second execution replayed the same
packets, adoption events, memory versions, and score identities.

These are sparse baseline scores, not continuations of the legacy Causa Prima
`91` journal. The disposable Vault had no pre-existing accepted v2 parent.
Consequently Causa Prima Magnetism is **0/10 here**, not 2/10. The human warning
is otherwise correct: this batch contributes no `MG*`, `C7`, `V3`, `VA4`, or
`I9` relation and therefore does not close that coverage gap.

`Backed by the world's best` may be investigated as an `MG10` candidate, but
the owned quote alone does not satisfy MG10: its contract requires observable
external investment/community/partner proof rather than an unsupported
traction claim.

## Narrow Causa Prima coverage supplement

Two bounded, non-authoritative supplement passes were run against the frozen
first Causa Prima normalized pack. The first froze 14 evidence/tile pairs. The
second added the stronger owned `MG1` literal `Agents do the work. You get the
money. In seconds.`, for 15 pairs. Both retained only four `MG10` relations and
left `C7`, `I9`, `MG1`, `MG3`, `MG5`, `V3`, and `VA4` uncovered. The second
result fingerprint is
`5f329620915bfe591be503f92500292ab8f8676784b8787de5a4383154434447`.

The four `MG10` proposals use four distinct external source identities: Cinco
Días, Munich Startup, The SaaS News, and Dealroom. No owned investment,
customer-count, investor-logo, or founder-traction claim is used as proof. The
provider's repeated omission of the explicit `MG1` pair, plus its omission of
`End the chase between companies` for `MG3`/`MG5`, is evidence that unattended
semantic proposal recall is not yet defensible. It is not evidence that those
tiles are absent.

A separate independent contract audit produced assessment-only candidates:

| Tile | Reviewed assessment | Basis / boundary |
|---|---|---|
| `MG1` | candidate | Concrete compressed work/money/speed promise |
| `MG3` | candidate | `End the chase...` names finance teams and tension |
| `MG5` | candidate | Short repeatable conflict-bearing verbal unit |
| `MG10` | candidate | Named third-party funding coverage only |
| `C7` | composite candidate | Owned web plus captured LinkedIn company profile; joint review required |
| `V3` | `sin_evidencia` | Competitor context exists, but the normalized pack lacks the required literal future proposition |
| `VA4` | `sin_evidencia` | Owned promises do not demonstrate the value in action |
| `I9` | `sin_evidencia` | The normalized pack lacks sufficient independent multi-surface breadth |

Reviewer `gsus` resolved both worksheets at
`2026-08-07T11:43:10+02:00`: all four exact `MG10` relations were accepted and
all eight proposed coverage assessments were confirmed. Immutable worksheet
columns still match their source artifacts exactly.

The independent assessment remains explicitly `adoption_eligible=false` even
after review. Its accepted `MG1`/`MG3`/`MG5` and composite `C7` assessments do
not authorize relations; `V3`/`VA4`/`I9` confirm scoped `sin_evidencia`, not
global absence. The noted VA4 pricing decision becomes investigable only when
its source pack is frozen.

### Disposable N+1 adoption

A dedicated coverage-supplement registration seam now derives the pending
80-tile source packet server-side from the exact artifact, normalized pack and
active Vault parent. It persists under the existing `operational_source_v2`
kind with a distinct content-bound coverage resolution. It does not fabricate a
capture operation and grants no source authority.

The four accepted `MG10` relations produced four durable human review events
and one Causa Prima N+1 adoption:

- parent: `1423ffd3fdce0e7e29d8adce16f91ad1b1f93be29a252aa6f545b809f50f78c4`;
- N+1: `b1bafe845ab85a888f71b044681eee69cd0548d0462a76d9288ecbc7074b887a`;
- adoption sequence: `2`, linked to the original sequence-1 event;
- accepted tiles: `P1`, `P3`, `P5`, `PR3`, `MG10`;
- accepted relations: `11` total, of which `4` are the new MG10 basis;
- sparse Vault score: **`4 → 6`**, with 5 accepted and 75 unresolved tiles.

MG10 is one tile and therefore one raw Magnetism point; the rubric's x2
Magnetism multiplier makes its score contribution two. The four publishers are
four durable source identities but cover one financing event. They do not
produce four tile hits or prove four independent traction events. Exact source,
review, adoption and score replay produced no additional rows or identities.
Production/scanner effects remain false. The final database was dumped,
restored successfully into a second disposable database, and then stopped. The
dump SHA-256 is
`422f5918b7d2b39250922e1c74717559e9f475c9b72e10813d08bd3fd9c4618b`.

## Exact relation and composite adoption

The confirmed assessment-only findings were projected into a separate,
content-bound exact-relation artifact without reusing the assessment decisions
as authority. It binds the Causa Prima sequence-2 parent
`b1bafe845ab85a888f71b044681eee69cd0548d0462a76d9288ecbc7074b887a`,
the normalized pack, reviewed assessment worksheet, and current registry,
reducer and aggregation policies. Its request fingerprint is
`718c88b886c110d767a767f619235046188f3ad9697ce8f6ab65b9ae197a4205` and
artifact fingerprint is
`13a1677eb3acfd2e51e8dd7be1c455be43e94358ad76cb79af0c88a6ce3ac795`.

The exact artifact contains five relations across four decision subjects:

- atomic `MG1`: `Agents do the work. You get the money. In seconds.`;
- atomic `MG3`: the exact two-paragraph `End the chase...` plus `finance teams`
  quote;
- atomic `MG5`: `End the chase between companies.`;
- composite `C7`: owned web plus captured LinkedIn company profile, under one
  two-member `all_of` group/claim.

Reviewer `gsus` accepted all four exact-scope subjects at
`2026-08-07T13:21:02+02:00`. Immutable worksheet columns rederived exactly from
the artifact. The completed worksheet hashes are:

- atomic MG review:
  `a1cdc29509ae720e89289c2b4620ceb8031093bea9a0a87f30d7de4112c12c11`;
- composite C7 review:
  `ed048d519004fd443007f406c7b2c1b93fc8331615290ace5d8df94e5ca26f68`.

C7 was not flattened into two independently authorizable tile hits. The source
manifest and immutable resolution carry its decision group. Repository and
pure projection checks require the complete pending set, identical decisions
and rationale for both members, and reject a split decision before any event
insert. The accepted group preserves two basis relations with two source
identities and one shared claim, while producing exactly one C7 tile hit.

A disposable sequence-3 adoption succeeded and replayed idempotently:

- parent:
  `b1bafe845ab85a888f71b044681eee69cd0548d0462a76d9288ecbc7074b887a`;
- canonical memory:
  `7b94b1062aa01b1cb259944b3cd67bef0fac614a130d90e65326b53d169b1a7f`;
- adoption event: `87d7285a-ca31-572a-98f6-c4bc9bcc7024`;
- previous event: `145f74b8-9d60-54be-9a5b-42ab5dc4aef6`;
- five new accepted relations and five append-only review events;
- accepted tiles: `C7`, `MG1`, `MG3`, `MG5`, `MG10`, `P1`, `P3`, `P5`,
  and `PR3`;
- sparse Vault score: **`6 → 14`**;
- 9 accepted and 71 unresolved tiles;
- tile authority coverage `0.1125`; score-weight coverage `0.14`.

The Magnetism interpretation must remain v2-sparse. Before this adoption the
Vault had one accepted Magnetism tile, `MG10`, worth two weighted points. It
now has exactly four: `MG1`, `MG3`, `MG5`, and `MG10`, for raw score 4 and
eight weighted points. `MG7` and `MG8` are legacy-context results and were not
accepted into v2, so the correct transition is not “2 to 5–6 Magnetism tiles.”
C7 adds one raw Coherencia hit and two weighted points. These contributions,
plus the prior four non-Magnetism points, derive the score 14.

The final database was dumped, restored into a second disposable database, and
verified at the same memory version, score and C7 group basis. The dump is
`/private/tmp/b3s-vault-causa-exact-relation-human-adoption-v1.dump`, SHA-256
`231c3e7f86f1221670e57c1503af9a432765b7f6cfb816b9a86043a11e5a7f56`.
Both databases and PostgreSQL were then stopped. Scanner/production effects
remain false.

The workflow that reacts to a later change in either accepted C7 member is
still a production blocker. `member_change_requires_review=true` is frozen in
the group contract, but it is not a substitute for implementing detection,
invalidation and group reopening.

## Legacy-to-v2 lineage gate

The frozen legacy `preview.json` cannot be used as a Vault-v2 parent. It is
`authority=false`, its identity reviews have no durable reviewer/event IDs, and
its score is diagnostic legacy output. The v2 builder also requires a parent
hash and accepted parent tiles together; the legacy artifact supplies neither a
valid v2 accepted-memory projection nor reviewed evidence-to-tile authority.
Sequence 1 is parentless by contract.

A focused anti-laundering test now asserts that the legacy memory hash cannot be
inserted as the v2 genesis parent. A separate parentless v2 genesis produces a
new canonical memory version and a new score evaluation derived from accepted
v2 basis; it does not import `91` or `93`.

A defensible continuity test therefore requires a new content-addressed,
human-reviewed evidence-to-tile seed/export. It must bind the full source
history, literal evidence and decision events, create a new parentless v2
memory, and keep the legacy hash only as non-authoritative provenance. The
normalized source packs for the three later legacy reports are not currently
frozen, so that seed does not yet exist.

## Hardening discovered by the field run

The initial field execution exposed issues that hermetic fixtures had not:

1. A real baseline exceeded the incremental 120-pair single-call bound.
   Baselines are now deterministically chunked into individually bounded calls;
   incremental refreshes retain the strict single-call ceiling.
2. The model abbreviated or stripped Markdown from quotes. It now receives
   server-derived literal quote candidates. Every retained quote still must be
   an exact substring of the frozen evidence.
3. The model can ignore an evidence-specific tile shortlist. Invalid individual
   relations are now discarded fail-closed, reason-coded, and hash-audited;
   valid rows can complete without importing the invalid row.
4. Broad semantic labels can exceed the per-evidence tile cap. Selection is now
   deterministic, forced incremental tiles take priority, and all omissions are
   persisted as an audit record.
5. Semantic labels are now first-class in the immutable result. PostgreSQL
   re-derives the shortlists and truncation audit from those labels before it
   accepts a result.
6. Relation IDs are now globally unique across every tile in a candidate
   packet. This prevents one reviewed relation event from authorizing a hidden
   duplicate occurrence on a second tile.
7. Model relation fields are type-strict before normalization; non-string
   quotes, rationales, tile IDs, polarities, or fingerprints are discarded.
8. The shadow writer validates the exact local URI route, rejects libpq host,
   database, service, and environment overrides, and requires an opt-in token
   equal to the disposable database name before migration or writes.
9. The first real N+1 human-review path exposed unsupported
   `replaces_coverage_refs` / `replaces_unresolved_refs` fields in three
   incremental builders. The canonical core already treats the presence of the
   reference arrays as replacement; the invalid flags were removed and an
   N+1 regression now covers preservation of the parent plus adoption of the
   new relation.
10. Composite C7 authority cannot be represented by ordinary independent
    support rows. The exact supplement now freezes an `all_of` group in source
    resolution and coverage metadata, and validates one matching decision for
    all members both before inserts and during pure reviewed projection.
11. The clean PostgreSQL 16 landing run exposed session-timezone-dependent
    serialization of durable review timestamps. Repository projections now
    normalize them to UTC while preserving the exact reviewed instant.
12. The release migration integration assertions ended at migration 013. They
    now require migrations 014–016, verify both operational tables, and
    enforce the complete 16-migration release path.
13. Incomplete semantic-label responses, deterministic identity mismatches, and
    non-evidentiary one-character quotes are now rejected or excluded at the
    bounded executor trust boundary instead of being neutral-filled or passed
    to relation review.
14. Expired `claimed`/`running` leases are now exposed as reclaimable work while
    active leases remain busy. The legacy scanner keeps its historical
    last-duplicate representative behavior; deterministic representative
    selection is isolated to the Vault helper.
15. Release migration execution is serialized by a database advisory lock.
    Migration 016 permits identical immutable source content to bind distinct
    operation results, and implicit human-review timestamps are recovered from
    durable review rows after a post-commit retry.
16. A committed audit verifier now enforces the selected implementation scope,
    every tracked audit artifact hash, the implementation fingerprint, the
    fixture binding, external checkpoint hashes when locally available, and the
    no-authority/no-cutover boundary.

## Validation evidence

- Original field worktree suite: **2639 passed, 19 skipped**.
- Clean landing hermetic suite: **2648 passed, 22 skipped**.
- Clean landing PostgreSQL 16 suite: **2669 passed, 1 skipped**.
- Coverage/exact-review/lineage focused suite: **25 passed**.
- Original disposable PostgreSQL focused suite: **12 passed**.
- Preserved exact-relation checkpoint forward migration to 016: passed.
- Preserved pre-coverage checkpoint all-reject review gate: passed with zero
  adoption, unchanged score 4, and `authority=false`.
- Audit manifest verifier with all three local external checkpoints required:
  passed.
- Ruff: passed.
- `git diff --check`: passed.
- Runtime wiring: `web/scan_runner.py` remains unchanged.

The PostgreSQL path covers capture persistence, canonical storage, operational
storage, leases/fencing, crash recovery, immutable results, concurrent
materialization, human relation review, baseline and N+1 adoption, idempotent
replay, explicit score creation from accepted memory, and successful restore of
the final disposable dump.

## Remaining production blockers

1. A fresh raw acquisition corpus is still required to validate
   `build_capture_observation_from_snapshot()` end to end.
2. The field run is an isolated sparse baseline. A legacy hash cannot be used
   as a v2 parent; the reviewed evidence-to-tile seed/export and three later
   normalized source packs needed for a real migration genesis are missing.
3. MG10, `MG1`/`MG3`/`MG5`, and the two-member C7 `all_of` group are
   reviewed and adopted only in the disposable Vault. A future accepted-C7
   member-change detection/invalidation/re-review workflow is still missing.
   `V3`, `VA4`, and `I9` remain `sin_evidencia` within the frozen
   normalized-pack boundary.
4. Production still lacks a feature flag, kill switch, and per-brand allowlist.
5. There is no public presentation seam that labels Vault memory/score as
   authoritative while keeping scanner diagnostics non-authoritative.
6. `diagnostic_full` remains an explicit scanner responsibility and is not wired
   into this Vault executor.
7. Contradiction-resolution policy needs an operational workflow before brands
   with accepted opposing basis can be activated safely.
8. The rejected model row, capped broad shortlist, and repeated omission of a
   strong explicitly shortlisted `MG1` literal still require semantic quality
   thresholds before unattended shadow expansion.

## Required next decision

Implement accepted-C7 member-change detection, invalidation and group reopening
before any C7 production use. The successful exact-scope disposable adoption
does not authorize runtime cutover.

Freeze the missing later source packs before reconsidering V3, VA4 or I9. The
VA4 pricing decision can be evaluated once its original source is present.
Separately, do not start legacy migration by reusing the `preview.json` memory
hash or score: construct the reviewed parentless v2 seed/export first.

Feature flags, presentation, and scanner cutover remain blocked. Production is
still NO-GO.
