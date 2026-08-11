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
every browser, legacy, diagnostic, report, brand, artifact, and provider-check
route. The dedicated `/vault/review/*` surface is the deliberate exception: it
uses only the evidence-reviewer credential, then an 8-hour signed
HttpOnly/SameSite=Strict session with CSRF protection. Unsafe requests require
an exact same-origin `Origin`; browsers that omit `Origin` may use an exact
same-origin `Referer` fallback, or the route's own signed CSRF token for the
login, logout, and decision POSTs. Only `/health` and `/api/v1/*`
otherwise bypass the site gate; versioned API routes retain their existing
bearer scopes.

Store the scanner bearer and reviewer bearer in an approved secret manager.
They must be fresh and the scanner and reviewer bearers must differ. Never
place credentials in this file, URLs, screenshots, PR comments, or command
history.

## PostgreSQL identities

`B3S_DATABASE_URL` is a non-owner runtime login. It may perform the explicit
application DML contract but cannot create/alter/drop schema objects and cannot
read the migration-019 raw provenance relations. At first access, runtime code
opens a repeatable-read, read-only transaction and requires the exact packaged
migration versions, filenames, and checksums. The isolated release verifier
also requires `current_user = b3s_pr71_app_runtime`; an owner or migration
credential on the same branch is rejected before the Machine update.

The isolated L2 logins are fixed and distinct:

- `b3s_pr71_app_runtime` — FastAPI/report runtime login
- `b3s_pr71_scanner_ingest` — worker-only execute-only raw append, exact replay, and bounded planning-context login;
  the worker preflight binds the session to this exact role
- `b3s_pr71_c7_runtime_read` — external private readiness login
- `b3s_pr71_c7_governance` — external governance/adoption capability

Provision these names in the isolated Neon branch only. Never substitute the
legacy generic role names from the reusable role runbook in this PR71 app.

`B3S_MIGRATION_DATABASE_URL` is a separately controlled migration identity. It
must never be installed with `fly secrets`; it exists only in the named operator
or protected GitHub `pr71-vault` release context. Run migrations before deploy:

```bash
B3S_MIGRATION_DATABASE_URL='...'   python scripts/migrate_b3s_history_postgres.py
B3S_MIGRATION_DATABASE_URL='...'   python scripts/configure_b3s_runtime_role.py --role b3s_pr71_app_runtime --database-name neondb
fly config validate -a b3s-pr71-vault -c fly.pr71-vault.toml
fly deploy --remote-only   -a b3s-pr71-vault   -c fly.pr71-vault.toml   --build-arg B3S_BUILD_SHA="$(git rev-parse HEAD)"
```

The role command is idempotent and only accepts the privileged URL through
`B3S_MIGRATION_DATABASE_URL`; it never prints that URL or a password.

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

## L2 worker secret files

Do not install the signer key, scanner-ingest DSN, or worker provider key with
`fly secrets`. The enabled supervisor rejects those four inline environment
variables. Provision them over the authenticated operator channel into the
encrypted isolated volume at the fixed paths below:

```text
/data/b3s-vault-worker/private-key.b64
/data/b3s-vault-worker/public-key-registry.json
/data/b3s-vault-worker/ingest-dsn
/data/b3s-vault-worker/exa-api-key
```

The directory must be root-owned mode `0700`; every file must be root-owned mode
`0600`, a regular non-symlink, and contain exactly one value with no trailing
newline. The volume root remains root-owned mode `1777`; its sticky bit prevents
the web uid from replacing the root-owned secret directory while leaving the
explicit report/SQLite paths writable. At startup, PID 1 validates stable file
metadata, copies the values to short-lived worker-owned files under `/run`,
starts the worker as `b3s-worker`, removes the `/run` files after the socket is
ready, and gives the web uid only group access to the `0660` Unix socket. The
persistent sources remain unreadable to `b3s` and must be removed when L2 ends.

`B3S_C7_RUNTIME_READ_DATABASE_URL` and the governance DSN remain external
operator capabilities; neither is installed in the Fly app or this volume.

## C7 boundary

The isolated L2 configuration enables only acquisition shadowing. Owned-web-only
analysis may continue when no external identity is available; its explicit
warning is non-qualifying and can never satisfy C7.

```text
B3S_VAULT_WORKER_ENABLED=true
BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED=true
BRAND3_VAULT_VERIFIED_RAW_ALLOW_OWNED_ONLY_ANALYSIS=true
BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH=
BRAND3_VAULT_C7_CUTOVER_ENABLED=false
BRAND3_VAULT_C7_EMERGENCY_DENY=true
BRAND3_VAULT_C7_ALLOWLIST=
```

The release Machine keeps the socket path empty; the supervisor injects its
validated dynamic `/run` socket only into the live web child. L2 still requires
two live captures 5 minutes–24 hours apart, review/adoption, governance bindings,
and a private readiness probe. C7 runtime remains a later independent gate even
if readiness becomes true.

## Rollback

Engage the access/C7 holds first, stop new scans, and drain the single active
scan before changing the Machine. Roll back to an exact recorded Fly image that
was verified against schema 021. Database rollback is never an in-place down
migration. Preserve both database states and reconcile deltas if any write
occurred after a Neon rollback anchor. The encrypted Fly volume and Neon branch
must be retained together for the declared incident window.
