# Evidence source/claim registry v2

## Verdict

The v2 projection makes the two review axes executable without replacing the
active source-independence registry or granting production authority.

It joins the frozen source-review dataset with the claim-corroboration queue
and preserves:

- publisher independence at exact source-URL scope;
- claim corroboration at exact `source_url + claim_id` scope;
- append-only review attribution from the source datasets;
- immutable dataset and event fingerprints;
- `runtime_effect=false` and `authority=false`.

Publisher independence never implies claim corroboration. A pending claim
never inherits the source-level aggregate `mixed` or `disputed` label.

## Current projection

Run:

```bash
./.venv/bin/python scripts/evidence_source_claim_registry.py
```

The current registry contains:

- 6 reviewed production sources;
- 5 `confirmed_independent` publishers and 1 `excluded` wire source;
- 10 claim-scoped records across 5 sources;
- 10 unique claim IDs;
- 0 reviewed claim decisions and 10 pending;
- 0 source-global corroboration decisions.

The registry fingerprint is deterministic and input-order invariant. A broken
dataset fingerprint, unknown parent source, source/claim URL mismatch, missing
`claim_id`, or incompatible publisher/claim decision fails closed.

## Adoption boundary

`shadow_contract_ready=true` means the data model is reproducible. It does not
mean the model is authorized for operational use.

`operational_adoption_ready=false` remains mandatory while:

- the 10 human claim decisions are pending;
- coverage contains only one real brand;
- semantic-paraphrase recall is unmeasured;
- the active identity projection still consumes
  `evidence_source_registry_v1.json`;
- no migration policy has been approved for downstream scoring or canonical
  claims.

The frozen claim-review manifest therefore remains unchanged with
`operational_contract.status=not_adopted`.
