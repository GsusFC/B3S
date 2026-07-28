# Evidence ledger shadow

## Verdict

The ledger is an experimental longitudinal read model, not a new source of
truth. It tests whether evidence can be remembered safely across scans without
letting acquisition failures or LLM variation rewrite the brand's observed
history.

It is enabled with:

```text
B3S_EVIDENCE_LEDGER_MODE=shadow
```

The default is `disabled`. Every payload declares `runtime_effect=false`.

## Authority boundary

The immutable scan reports remain the source of record. The ledger is fully
recomputable from them and cannot:

- alter a score, interpretation, or report;
- select or replace a canonical scan;
- mark a fact as validated;
- retire evidence after a missing acquisition;
- infer a semantic contradiction from changed text;
- treat repeated or syndicated content as independent corroboration.

If its PostgreSQL rebuild fails, a nested transaction/savepoint rolls back only
the shadow projection. The authoritative report import continues.

## States

| State | Meaning |
| --- | --- |
| `observed` | Exact normalized evidence appeared once in the latest capture. |
| `repeated` | It appeared exactly in multiple captures, but validation eligibility is incomplete. |
| `validation_candidate` | It appeared exactly in at least two eligible captures. This is only a review proposal. |
| `not_reacquired` | It was seen earlier but not acquired in the latest capture. It remains remembered. |
| `changed_candidate` | The same normalized locator produced different content. No semantic contradiction is assumed. |
| `stale_candidate` | It was not reacquired and its source-class shadow TTL elapsed. Nothing is retired automatically. |

Owned evidence is eligible only when its URL belongs to the scanned brand
domain or a subdomain. External evidence is eligible only when identity
matching resolves to `domain` or `brand_name`. Visual and derived evidence can
be observed and compared, but never become automatic validation candidates.

## Persistence

Migration `003_evidence_ledger_shadow.sql` adds three tables:

- `evidence_ledger_shadow_states`: current recomputable payload per brand;
- `evidence_ledger_shadow_entries`: exact evidence identities and proposed
  states;
- `evidence_ledger_shadow_observations`: capture/report observations behind
  each entry.

Raw evidence content is not duplicated into these tables. The projection keeps
normalized fingerprints, content hashes, locators, source metadata, timestamps,
and report references. Original content remains in immutable report snapshots.

The read endpoint is:

```text
GET /api/v1/brands/{domain}/evidence-ledger-shadow
```

`persistence.stored=true` means PostgreSQL has the projection matching the
current immutable history. Otherwise the API returns a safe in-memory
recomputation with `backend=history_derived`.

After Fly verifies the deployed commit, the deployment workflow backfills every
existing brand directly from PostgreSQL report snapshots. Each brand uses the
same advisory lock and savepoint isolation as a normal report import. The step
has a five-minute timeout and `continue-on-error`; backfill failures are
reported but cannot fail the deployment because the projection has no runtime
authority.

## Shadow evaluation

The first evaluation period should answer these questions with reviewed
examples, not only aggregate counts:

1. Do `validation_candidate` entries really belong to the brand, especially
   for common names?
2. How often does `not_reacquired` coincide with provider or crawl degradation
   rather than a real disappearance?
3. How many `changed_candidate` entries are caused by dynamic page fragments,
   dates, counters, or extraction boundaries?
4. Are the source-class TTL suggestions useful, or do they mostly create false
   staleness?
5. Does rebuilding the projection remain fast and reliable as histories grow?
6. Does an LLM identity-label change alter eligibility without changing the
   underlying evidence identity, as intended?

No state should gain runtime authority until reviewed cases establish an
explicit precision target, an appeal/reversal path, and a versioned promotion
policy. Promotion would require a new policy and schema version; it must not be
implemented by changing the meaning of these shadow states.

## Rollback

Set:

```text
B3S_EVIDENCE_LEDGER_MODE=disabled
```

This stops computation and persistence immediately. Existing shadow rows can
remain for later analysis because no runtime path consults them for scoring or
canonical selection.
