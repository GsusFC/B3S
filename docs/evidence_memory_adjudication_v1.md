# Evidence memory identity adjudication v1

## Verdict

The adjudication journal closes the reversible identity-decision part of Gate
1. It does not make evidence, claims, tiles, reports, or scores canonical.

An `accepted` event has one narrow meaning: a named reviewer accepted that the
evidence subject refers to the scanned brand. It is not a truth verdict and
does not turn `repeated` into `validation_candidate`.

## Durable contract

Events are appended to
`b3s_history.evidence_memory_adjudication_events`. Every event records:

- the immutable evidence subject ID;
- `accepted`, `disputed`, `rejected`, or `revoked`;
- its per-subject sequence and predecessor event;
- schema, policy, and evaluator versions;
- server-derived reviewer and authenticated API actor;
- reason code, rationale, and creation time;
- idempotency-key hash and normalized request fingerprint;
- `runtime_effect=false` and `authority=false`.

Older events are returned with effective state `superseded`; the stored
decision itself is never rewritten. A `revoked` event can only follow a current
non-revoked decision.

The database constrains a predecessor to the same brand and evidence subject.
Application writes serialize per subject and reject stale
`expected_current_event_id` values.

## Write protocol

```text
POST /api/v1/brands/{domain}/evidence-memory-adjudications
Authorization: Bearer …
Idempotency-Key: stable-client-key
```

First decision:

```json
{
  "subject_id": "<64-char evidence id>",
  "decision": "accepted",
  "expected_current_event_id": null,
  "reason_code": "identity_confirmed",
  "rationale": "The source identifies the scanned brand unambiguously.",
  "evaluator_version": "manual-review-v1"
}
```

The Bearer token for this endpoint is configured separately as
`B3S_EVIDENCE_ADJUDICATION_TOKEN`, and the server records
`B3S_EVIDENCE_REVIEWER_ID` as both reviewer and actor. A request cannot declare
or impersonate another reviewer.

To change or revoke a decision, the caller must send the current event ID in
`expected_current_event_id`. Reusing an idempotency key with the same normalized
request replays the original event. Reusing it for a different request returns
`409`.

Writes return `503` when PostgreSQL is absent or unavailable. There is
deliberately no report-file fallback for mutable decisions.

## Read protocol

```text
GET /api/v1/brands/{domain}/evidence-memory-adjudications
GET /api/v1/brands/{domain}/evidence-memory-adjudications?subject_id=<id>
GET /api/v1/brands/{domain}/evidence-memory-identity-v2-shadow
```

The journal endpoint returns both the reviewable event page and the current
decision per subject. Identity v2 overlays those current decisions and includes
them in its deterministic state fingerprint.

## Remaining safety limits

- The current deployment supports one server-configured reviewer rather than a
  multi-user identity system.
- Only evidence-to-brand identity is adjudicated.
- Semantic claim replacement and claim truth are not adjudicated.
- No adjudication affects scoring, report selection, or tiles.
- There is no reviewed gold set yet.
