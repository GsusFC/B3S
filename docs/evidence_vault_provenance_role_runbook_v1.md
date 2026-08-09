# Evidence Vault verified-raw PostgreSQL role runbook v1

Status: deployment contract for Draft PR #70. Production remains **NO-GO**.

Migration 019 deliberately does not create application `LOGIN` roles or embed credentials. Provision them in the deployment control plane. Never reuse the FastAPI/report DSN as scanner ingest, governance, or release migration identity.

## Role split

| Identity | LOGIN | Purpose | Allowed Vault surface |
|---|---:|---|---|
| `b3s_history_vault_provenance_owner` | no | owns migration-019 journals and definer functions | no direct connection |
| release migrator | yes, controlled | applies immutable migrations | release window only; may `SET ROLE` to owner |
| scanner ingest | yes | isolated acquisition worker | execute raw append and raw replay-read functions only |
| runtime read | yes | public-key verification/shadow readiness reader | execute read projections only |
| provenance governance | yes | reviewed lineage binding and retention/revocation | execute bind and disposition functions only |

The acquisition worker is the only process that receives the scanner-ingest DSN and Ed25519 private key. FastAPI/report/runtime receive neither.

## Before migration 019

Unless the release migrator is explicitly allowed to create the fixed owner, a cluster role administrator **must** pre-provision it and grant the controlled migrator SET capability before migration 019. Migration 019 fails rather than leaving privileged objects owned by a LOGIN role:

```sql
CREATE ROLE b3s_history_vault_provenance_owner NOLOGIN
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
GRANT b3s_history_vault_provenance_owner TO release_migrator;
```

On PostgreSQL 16+, verify `SELECT pg_has_role('release_migrator', 'b3s_history_vault_provenance_owner', 'SET')`; on PostgreSQL 14/15 verify `MEMBER`. A membership with `SET FALSE` is deliberately insufficient. Use the platform secret manager for every `LOGIN` credential. Do not put passwords, private keys, or DSNs in repository SQL.

## After migration 019

Create deployment-specific login roles (names below are examples), then grant only schema use and exact function execution:

```sql
CREATE ROLE b3s_vault_scanner_ingest LOGIN NOINHERIT
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE b3s_vault_runtime_read LOGIN NOINHERIT
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
CREATE ROLE b3s_vault_provenance_governance LOGIN NOINHERIT
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

GRANT CONNECT ON DATABASE :"database_name" TO
  b3s_vault_scanner_ingest,
  b3s_vault_runtime_read,
  b3s_vault_provenance_governance;
GRANT USAGE ON SCHEMA b3s_history TO
  b3s_vault_scanner_ingest,
  b3s_vault_runtime_read,
  b3s_vault_provenance_governance;

GRANT EXECUTE ON FUNCTION
  b3s_history.append_evidence_vault_raw_acquisition(jsonb),
  b3s_history.read_evidence_vault_raw_acquisition(text, text)
TO b3s_vault_scanner_ingest;

GRANT EXECUTE ON FUNCTION
  b3s_history.read_evidence_vault_raw_provenance(uuid)
TO b3s_vault_runtime_read;

GRANT EXECUTE ON FUNCTION
  b3s_history.bind_evidence_vault_verified_c7_lineage(jsonb),
  b3s_history.append_evidence_vault_raw_provenance_disposition(jsonb)
TO b3s_vault_provenance_governance;

ALTER ROLE b3s_vault_runtime_read SET default_transaction_read_only = on;
```

Run the block through `psql -v database_name=...`; `:"database_name"` is a quoted psql identifier variable, not a SQL function.

## Mandatory negative grants

Application roles receive no ownership, membership in the NOLOGIN owner, schema `CREATE`, `SET ROLE`, DDL, or direct table DML:

```sql
REVOKE ALL ON ALL TABLES IN SCHEMA b3s_history FROM
  b3s_vault_scanner_ingest,
  b3s_vault_runtime_read,
  b3s_vault_provenance_governance;
REVOKE CREATE ON SCHEMA b3s_history FROM
  b3s_vault_scanner_ingest,
  b3s_vault_runtime_read,
  b3s_vault_provenance_governance;
```

Apply the exact function grants again after those table revocations. Verify with `has_table_privilege` and `has_function_privilege` from each login. Scanner/governance functions are `SECURITY DEFINER` with fixed `search_path`; their owner remains NOLOGIN. After release validation, revoke the owner membership from the release migrator and re-grant it only inside a later controlled migration window.

## Operational rules

- Start the acquisition worker under a separate OS/container identity. Inject its private key and scanner DSN only there.
- A worker retry performs durable replay lookup before network collection or signing.
- Runtime/read transactions are read-only and reverify Ed25519, snapshot, extractor, passage, lineage, freshness, registry, and disposition state in Python.
- Legal hold and `revoke_runtime` are logical append-only dispositions. This release makes no physical purge or crypto-shred claim.
- Do not grant any production role until the final authorization review. Draft PR #70 itself does not enable C7 runtime, API, scanner effects, scoring, or presentation.
