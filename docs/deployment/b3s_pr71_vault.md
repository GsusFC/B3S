# Isolated PR71 Vault on Fly

`https://b3s-pr71-vault.fly.dev` is a third, isolated environment for operating
and validating the PR71 Vault stack. It is not `b3s`, not `b3s-vault`, and does
not authorize either existing app, production migration, or C7 cutover.

## Fixed identity

- Fly app: `b3s-pr71-vault`
- Fly config: `fly.pr71-vault.toml`
- Fly volume: `b3s_pr71_vault_data` in `cdg`
- Neon project: `jolly-river-32467750`
- Neon branch: `br-divine-star-aspobuer` (`b3s-pr71-vault`)
- Parent: `br-misty-sky-asfp14gb` (`b3s-vault`)
- Public hostname: `b3s-pr71-vault.fly.dev`

The release command runs `verify_release_build.py`, the SELECT-only exact schema
head verifier, and `verify_pr71_vault_target.py`. The final verifier checks the
Fly app, base URL, database/project/branch identity, access gate, and denied C7
controls. A mismatch aborts before an app Machine is updated.

## Access boundary

The isolated app sets `B3S_SITE_BASIC_AUTH_ENABLED=true`. HTTP Basic protects
every browser, legacy, diagnostic, report, brand, artifact, reviewer, and
provider-check route. Only `/health` and `/api/v1/*` bypass the site gate;
versioned API routes retain their existing bearer scopes. Unsafe browser
requests additionally require an exact same-origin `Origin` header.

Store the site password, scanner bearer, and reviewer bearer in an approved
secret manager. They must be fresh and the scanner and reviewer bearers must
differ. Never place credentials in this file, URLs, screenshots, PR comments,
or command history.

## PostgreSQL identities

`B3S_DATABASE_URL` is a non-owner runtime login. It may perform the explicit
application DML contract but cannot create/alter/drop schema objects and cannot
read the migration-019 raw provenance relations. At first access, runtime code
opens a repeatable-read, read-only transaction and requires the exact packaged
migration versions, filenames, and checksums.

`B3S_MIGRATION_DATABASE_URL` is a separately controlled migration identity. It
must never be installed with `fly secrets`; it exists only in the named operator
or protected GitHub `pr71-vault` release context. Run migrations before deploy:

```bash
B3S_MIGRATION_DATABASE_URL='...'   python scripts/migrate_b3s_history_postgres.py
fly config validate -a b3s-pr71-vault -c fly.pr71-vault.toml
fly deploy --remote-only   -a b3s-pr71-vault   -c fly.pr71-vault.toml   --build-arg B3S_BUILD_SHA="$(git rev-parse HEAD)"
```

Do not use `fly.toml`, `fly.vault.toml`, `b3s`, `b3s-vault`, their volumes, or
their database branches in this workflow. The manual GitHub workflow requires
the operator to type `b3s-pr71-vault` and provide the exact deployment SHA.

## Required app secrets

L1 operation requires only app-scoped credentials:

- `B3S_DATABASE_URL` — isolated least-privilege runtime DSN
- `B3S_SITE_BASIC_AUTH_PASSWORD` — browser/legacy site gate
- `B3S_SCANNER_API_TOKEN` — versioned scanner API bearer
- `B3S_EVIDENCE_ADJUDICATION_TOKEN` — distinct reviewer/adjudication bearer
- provider credentials required by the standard scanner (`BRAND3_LLM_*`,
  `EXA_API_KEY(S)`, and `FIRECRAWL_API_KEY(S)`)

Worker signing keys, scanner-ingest DSNs, governance DSNs, runtime-read DSNs,
and the external migrator DSN do not belong in the web process.

## C7 boundary

The always-on L1 service keeps:

```text
BRAND3_VAULT_C7_CUTOVER_ENABLED=false
BRAND3_VAULT_C7_EMERGENCY_DENY=true
BRAND3_VAULT_C7_ALLOWLIST=
BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED=false
BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH=
```

Verified-raw worker operation is a later L2 gate. It requires separate OS and
PostgreSQL identities, worker-only files, a supervised same-Machine Unix socket,
two live captures 5 minutes–24 hours apart, human review/adoption, governance
bindings, and a private readiness probe. C7 runtime remains a later independent
gate even if readiness becomes true.

## Rollback

Engage the access/C7 holds first, stop new scans, and drain the single active
scan before changing the Machine. Roll back to an exact recorded Fly image that
was verified against schema 021. Database rollback is never an in-place down
migration. Preserve both database states and reconcile deltas if any write
occurred after a Neon rollback anchor. The encrypted Fly volume and Neon branch
must be retained together for the declared incident window.
