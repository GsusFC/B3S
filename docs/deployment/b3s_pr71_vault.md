# Isolated PR71 Vault on Fly

`https://b3s-pr71-vault.fly.dev` is a third, isolated environment for operating
and validating the PR71 Vault stack. It is not `b3s`, not `b3s-vault`, and does
not authorize either existing app, a production migration, or any later deployment.

## Fixed identity

- Fly app: `b3s-pr71-vault`
- Fly config: `fly.pr71-vault.toml`
- Fly volume: `b3s_pr71_vault_data` in `cdg`
- Neon project: `jolly-river-32467750`
- Neon branch: `br-divine-star-aspobuer` (`b3s-pr71-vault`)
- Database: `neondb`
- Release migrator `current_user`: `neondb_owner`
- Parent: `br-misty-sky-asfp14gb` (`b3s-vault`)
- Public hostname: `b3s-pr71-vault.fly.dev`

The release command runs `verify_release_build.py`, the SELECT-only exact schema
head-024 verifier, and `verify_pr71_vault_target.py`. The final verifier checks
the Fly app, base URL, authenticated TLS session, exact host/database/runtime
role/project/branch identity, the operational-pipeline capability, and access
gate. A mismatch aborts before an app Machine is updated. The
release command never applies DDL with the runtime `B3S_DATABASE_URL`.

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

Migration 024 preserves the strict replay behavior from migration 023 and additionally projects exact active accepted evidence relations into fresh planning. Migration 023 keeps the worker replay reader strict after a report upgrades mutable `scan_runs` presentation fields: it reconstructs the frozen raw scan projection from the immutable request and plan, while retaining current source-run/status/error/requested/started and non-lifecycle metadata so contamination still fails closed.

The isolated L2 logins are fixed and distinct:

- `b3s_pr71_app_runtime` — FastAPI/report runtime login
- `b3s_pr71_scanner_ingest` — worker-only execute-only raw append, exact replay, and bounded planning-context login;
  the worker preflight binds the session to this exact role
- `b3s_pr71_c7_runtime_read` — external private provenance-diagnostic login
- `b3s_pr71_c7_governance` — external governance/adoption capability

Provision these names in the isolated Neon branch only. Never substitute the
legacy generic role names from the reusable role runbook in this PR71 app.

`B3S_MIGRATION_DATABASE_URL` is the separately controlled `neondb_owner`
migration identity. It must contain a password and use either
`sslmode=verify-full` or the deployed
`sslmode=require&channel_binding=require` contract, and
must never be installed with `fly secrets`; it exists only for the controlled
external migration invocation. Before its first advisory lock or DDL, the
migrator validates the authenticated URL, requires
`connection.pgconn.ssl_in_use is True` from libpq on the **same connection**,
requires its exact connected host, and then SELECTs the exact database,
`current_user`, Neon project, and Neon branch identity. Missing or inactive client TLS and any identity mismatch
perform no migration DDL. Neon terminates client TLS at its proxy, so the
backend-facing `pg_stat_ssl.ssl` value is not evidence of the client connection's
TLS state and is not part of this target attestation. Run migrations before deploy
only from a clean operator environment where the approved secret manager has
already injected `B3S_MIGRATION_DATABASE_URL`. Do not paste, assign, or export
the DSN inline in a command that can enter shell history:

```bash
test -n "${B3S_MIGRATION_DATABASE_URL:-}" || {
  echo "B3S_MIGRATION_DATABASE_URL is not loaded" >&2
  exit 1
}
python scripts/migrate_b3s_history_postgres.py --target-profile pr71-vault
python scripts/configure_b3s_runtime_role.py \
  --target-profile pr71-vault \
  --role b3s_pr71_app_runtime \
  --database-name neondb
fly config validate -a b3s-pr71-vault -c fly.pr71-vault.toml
fly deploy --remote-only \
  -a b3s-pr71-vault \
  -c fly.pr71-vault.toml \
  --build-arg B3S_BUILD_SHA="$(git rev-parse HEAD)"
```

The role command repeats the same target assertion on its own grant connection
before any `REVOKE`/`GRANT`, is idempotent, and only accepts the privileged URL
through `B3S_MIGRATION_DATABASE_URL`. Before its positive grants it revokes the
PostgreSQL default `TEMPORARY` database privilege from `PUBLIC`, then verifies
the runtime's effective database boundary as `CONNECT=true`, `CREATE=false`, and
`TEMPORARY=false`. The database owner retains its implicit owner capabilities,
so this database-wide `PUBLIC` revocation does not disable the controlled
`neondb_owner` migrator. The exact runtime role must already exist as a
password-bearing `LOGIN`; the command never creates it, receives or changes its
password, or prints a DSN/driver error.

## Post-merge deployment gate

Do not use `fly.toml`, `fly.vault.toml`, `b3s`, `b3s-vault`, their volumes, or
their database branches in this workflow. The manual GitHub workflow is a
**post-merge-only** deployment mechanism for the isolated `b3s-pr71-vault`
target. Its current reviewed application baseline is PR #76 at head
`4210f743660b57cf760a91b353dc775019c9dc90` and merge commit
`5c4c737a17e46c0dc571132b5bd1dfdd63c687cc`. Neither merging the reviewed
PR, the presence of `workflow_dispatch`, the confirmation input, nor a
successful attestation authorizes a merge or deployment. Merge approval and
the deployment GO remain separate operator decisions.

Dispatch only from exact `refs/heads/main` through the GitHub environment
`pr71-vault`. The input SHA must equal both the immutable dispatch SHA and the
live REST `main` ref. The environment has a custom deployment branch policy configured to allow only `main`.

The `pr71-vault` environment currently contains zero deployment secrets.
`B3S_MIGRATION_DATABASE_URL` and `FLY_API_TOKEN` were provisioned only for the
previous authorized deployment and then removed. Dispatch `31507333105` failed closed before checkout or secret use; retry `31526043212` failed before advisory lock, DDL, ACL, or Fly mutation. Final run `31534878673` completed every attestation, migration/ACL, deploy, and exact-commit liveness step successfully. Both temporary deployment secrets were then removed.
Any future deployment requires a new exact-SHA GO and fresh secret provisioning.

Before checkout, dependency installation, or deployment-secret use,
runner-owned code fetches PR #76 and requires `state=closed`, `merged=true`, and `draft=false`, base `main`, head `fix/c7-functional-nonblocking`, repository ID
`1288696741` and name `GsusFC/B3S`, exact reviewed head, and exact `merge_commit_sha`.
It proves reviewed-head-to-merge and merge-to-current-main ancestry from complete
GitHub Compare responses. For the deployment-attestation follow-up, the latter
comparison must contain the exact three audited files listed above; those are these existing, modified, non-renamed paths:

- `.github/workflows/fly-deploy-pr71-vault.yml`
- `docs/deployment/b3s_pr71_vault.md`
- `tests/test_deploy_provenance.py`

Any runtime, database, CI-workflow, config, or other source change after PR #76
is outside the attestation allowlist and fails closed.

The trusted CI identity is pinned to workflow ID `306885838`, path
`.github/workflows/ci.yml`, and blob
`c1082f6b5c53a364b8d38e43936b93722c0183d1`. The workflow must remain active;
the reviewed head, merge, and deployment Contents responses must carry that
exact blob. Exact successful CI runs `31567629360` (reviewed PR #76 head) and
`31568953356` (PR #76 merge on `main`) are pinned. The current exact-main push run is fetched without filtering away non-successful runs; exactly one completed,
successful, first-attempt run with the expected repository and SHA is accepted.
The `pull_requests` array may be empty. Missing, pending, failed, foreign, stale,
wrong-event, workflow-modified, truncated, or ambiguous objects fail closed.

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

## C7 and provenance-diagnostic boundary

C7 is a normal functional and scored tile. Owned-web-only analysis may continue
when no external identity is available; without the second reviewed channel C7
uses its ordinary unresolved/pending evidence state and the scan still completes.

```text
BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED=true
B3S_VAULT_WORKER_ENABLED=true
BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SHADOW_ENABLED=true
BRAND3_VAULT_VERIFIED_RAW_ALLOW_OWNED_ONLY_ANALYSIS=true
BRAND3_VAULT_VERIFIED_RAW_ACQUISITION_SOCKET_PATH=
```

The release Machine keeps the socket path empty; the supervisor injects its
validated dynamic `/run` socket only into the live web child. Two live captures,
review/adoption, governance bindings, and the private probe apply only when an
operator wants the optional verified-raw diagnostic result. They do not enable
or deny C7 and are never a deployment or publication gate.

## Rollback and NO-GO

Deployment is **NO-GO** unless the fixed PR #76 reviewed head and merge attest,
the requested SHA equals both dispatched and live current `main`, the cumulative
post-PR76 changes are the exact three audited files listed above, all three CI
blobs and all required CI runs attest, the separate deployment GO and secret
provisioning are complete, the external migration target assertion passes before
DDL, head `024` verifies exactly, and the idempotent runtime-role contract passes.
`b3s` and `b3s-vault` must remain on their SELECT-only release verifiers and may
not be used as fallback migration targets.

No older application image is assumed compatible with schema head `024`, and
this runbook pre-authorizes no image rollback. Engage the access and scan holds first,
stop new scans, and drain the single active scan. Prefer a reviewed forward fix;
any coordinated image-plus-Neon-restore plan requires separate authorization and
proof against a preserved restore anchor. A release verifier mismatch is a hard
stop, not permission to run an older migrator. Database rollback is never an
in-place down migration. Preserve both database states and reconcile deltas if
any write occurred after a Neon rollback anchor. The encrypted Fly volume and
Neon branch must be retained together for the declared incident window.
