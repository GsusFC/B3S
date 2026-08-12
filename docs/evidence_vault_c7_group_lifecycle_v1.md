# Evidence Vault C7 `all_of` lifecycle v1

Status: **Vault-only control implemented; production cutover remains NO-GO.**

This stacked change closes the missing accepted-group invalidation/reopen
primitive documented in `evidence_vault_canonical_memory_adr_v2.md`. It does
not enable the scanner, production scoring, or any production route.

## Authority boundary

C7 authority is a versioned group, not a pair of independent relation rows and
not a bare `claim_id`. The durable exact-relation source must rederive:

- one `decision_rule=all_of` C7 group;
- its exact member relation IDs;
- the frozen evidence fingerprints and source identities;
- distinct `owned_web` and `external_social_profile` channel roles;
- matching accepted relation-review decisions; and
- the canonical parent under which the group was accepted.

A transition fails closed if any member, review event, source resolution, or
parent cannot be rederived.

## Trigger semantics

### Acquisition absence

`not_reacquired` remains coverage loss only. It cannot remove, refute, or
reopen an accepted group. No score change is authorized from absence alone.

### Material member change

A completed durable operation plan may reopen C7 only when its content-addressed
incremental delta lists a frozen group member in
`superseded_evidence_fingerprints`. The transition:

1. resolves the active group through the immutable exact-relation source;
2. appends one content-addressed `group_reopened` source resolution;
3. supersedes every old member relation in one pending source packet;
4. removes C7 from current accepted authority;
5. persists a `pending_reassessment` descriptor containing the old group,
   trigger, policy, and exact superseded-member evidence fingerprints; and
6. adopts the score-lowering N+1 memory under the existing brand advisory lock
   and canonical-parent compare-and-swap.

The operation plan is already durable before this method runs. A concurrent
loser therefore remains recorded and must re-read/rebase; its observation is
not lost.

### Accepted contradiction

An accepted contradictory relation-review event is authority for that exact
observation only. It does not authorize C7. The same repository transition can
consume the immutable review event only when its `source_identity_id` resolves
to exactly one frozen group member. The affected old evidence fingerprint is
rederived during artifact validation and persisted in the pending descriptor;
a caller cannot substitute another member. The transition reopens the complete
old group. A reject or an unrelated C7 contradiction does not qualify.

## Persistence and idempotency

No mutable group state or new table is introduced. The lifecycle journal reuses
the append-only canonical packet and promotion ledgers:

- the `operational_source_v2` reference resolution contains the complete reopen
  artifact and durable trigger binding;
- the source packet row ID is the deterministic reopen event UUID;
- the `operational_v2` packet removes current C7 authority and carries the
  pending reassessment; and
- one `operational_v2` adoption activates N+1.

The transaction holds `evidence-vault-canonical-promotion` for the brand.
Replay identity binds brand, old group, durable trigger, and canonical parent.
Exact retries return the existing source, packet, adoption, and projected
memory. A different stale trigger is rejected after the first transition.
Existing immutable-ledger triggers reject `UPDATE` and `DELETE`.

## Replacement group resolution

The pending descriptor survives unrelated packet projections and score
recomputation. It cannot disappear silently. Clearing it requires an accepted
tile backed by a new exact `all_of` source whose:

- group ID differs from the prior group;
- complete member set is present in both reviewed source and accepted memory;
- every member is accepted and linked to a durable decision event;
- no replacement member reuses a superseded evidence fingerprint recorded by
  the immutable reopen artifact; and
- parent is the current reopened memory.

The storage boundary derives the superseded set from the immutable artifact and
requires the pending descriptor to match it exactly. Thus an old member review
cannot leak into a replacement group. The existing
exact-relation matching-decision rule remains the explicit human group
resolution event.

## Score/read behavior

While pending:

- C7 is absent from `accepted_tiles`;
- its effective points are zero;
- `score_eligible=false`;
- `pending_change_tile_count` includes C7; and
- both the memory projection and a fresh operational score evaluation report
  `canonical_score_status=pending_reassessment`.

Historical packets and accepted reviews remain immutable and queryable; only
the current authority projection changes.

## API

`PostgresHistoryRepository.reopen_evidence_vault_composite_group(...)` accepts
exactly one durable trigger:

- `source_scan_id` for a completed material-change operation plan; or
- `accepted_contradiction_review_event_id` for an accepted contradictory
  relation review.

The caller must also supply the original exact source packet fingerprint.

## Validation in this branch

The regression set covers:

- material change of each C7 member;
- whole-group invalidation and two-point score suppression;
- `not_reacquired`, identical capture, and unrelated change no-op behavior;
- accepted-versus-rejected contradiction semantics;
- partial/drifted group fail-closed behavior;
- deterministic replay and immutable source resolution;
- pending reassessment survival across later projections;
- PostgreSQL atomic source/packet/adoption persistence;
- exact retry, stale competing trigger, and durable loser observation; and
- journal `UPDATE`/`DELETE` rejection.

## Product boundary

This lifecycle is the ordinary functional lifecycle for C7. A material member
change reopens C7 and temporarily changes only its tile state and normal
Coherencia contribution; it never blocks the surrounding scan, persistence,
report, API, UI, or deployment.

See the active
[`evidence_vault_c7_product_contract_v1.md`](evidence_vault_c7_product_contract_v1.md).
The former C7-specific runtime cutover control plane is retired.
