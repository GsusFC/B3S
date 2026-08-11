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
- Database: `neondb`
- Release migrator `current_user`: `neondb_owner`
- Parent: `br-misty-sky-asfp14gb` (`b3s-vault`)
- Public hostname: `b3s-pr71-vault.fly.dev`

The release command runs `verify_release_build.py`, the SELECT-only exact schema
head-023 verifier, and `verify_pr71_vault_target.py`. The final verifier checks
the Fly app, base URL, authenticated TLS session, exact host/database/runtime
role/project/branch identity, the operational-pipeline capability, access gate,
and denied C7 controls. A mismatch aborts before an app Machine is updated. The
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

Migration 023 keeps the worker replay reader strict after a report upgrades mutable `scan_runs` presentation fields: it reconstructs the frozen raw scan projection from the immutable request and plan, while retaining current source-run/status/error/requested/started and non-lifecycle metadata so contamination still fails closed.

The isolated L2 logins are fixed and distinct:

- `b3s_pr71_app_runtime` — FastAPI/report runtime login
- `b3s_pr71_scanner_ingest` — worker-only execute-only raw append, exact replay, and bounded planning-context login;
  the worker preflight binds the session to this exact role
- `b3s_pr71_c7_runtime_read` — external private readiness login
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
their database branches in this workflow. The manual GitHub workflow is a **post-merge-only** deployment mechanism: the
workflow file was not registered on the default branch before PR #71 landed.
PR #71 is fixed at reviewed head
`377364784ee9b4554acd074a7013e870191654e0` and merge commit
`05393b5eac7fa7be68f5739eba92becb40619a50`. The post-merge attestation hotfix
may be dispatched only after it lands on `main`; provide the full 40-character
SHA of that then-current `main`, not the older PR #71 merge SHA, and type
`b3s-pr71-vault`. Neither merging PR #71, merging the attestation hotfix, the
presence of `workflow_dispatch`, the confirmation input, nor a successful
attestation authorizes a merge or a deployment. Merge approval and the
deployment GO remain separate operator decisions.

Dispatch only from the exact Git ref `refs/heads/main` and only through the
GitHub environment `pr71-vault`. The job condition and the pre-checkout
attestation both require `github.ref == refs/heads/main`; the input SHA must
also equal both the immutable workflow-dispatch `github.sha` and the live REST
`refs/heads/main` commit. The environment has a custom deployment branch policy configured to allow only `main`;
retaining and verifying that external policy
immediately before dispatch is mandatory. A caller can request
`workflow_dispatch` with another `ref`, and code loaded from that mutable ref
could remove an in-workflow check, so the environment policy is part of the
boundary rather than optional duplication.

The `pr71-vault` environment now contains exactly
`B3S_MIGRATION_DATABASE_URL` and `FLY_API_TOKEN`, provisioned only after the
owner authorized the isolated deployment. The first dispatch (`31507333105`)
failed closed in pre-checkout attestation, before either secret expression or
any database/Fly mutation. Authorized retry `31526043212` passed the complete
workflow attestation, checkout, and installation, then failed closed in the
migration target preflight before any DDL, role grants, or Fly mutation. A
read-only diagnostic on that same target proved the exact database, user,
project, and branch plus `connection.pgconn.ssl_in_use is True`; Neon reported
`pg_stat_ssl.ssl=false` because its proxy terminates the client TLS connection.
Retain those secrets only for an explicitly authorized retry of the exact
hotfix/current-main SHA; remove them if that retry is abandoned. Secret
expressions occur only in steps after the complete attestation, checkout, and
package installation.

Before checkout, dependency installation, or deployment-secret use,
runner-owned code fetches PR #71 through the GitHub REST API and requires
`state=closed`, `merged=true`, and `draft=false`. It requires base ref `main`,
head ref `fix/vault-v2-cumulative-landing`, exact repository ID `1288696741`
and name `GsusFC/B3S` in both `base.repo` and `head.repo`, exact `head.sha`
`377364784ee9b4554acd074a7013e870191654e0`, and exact `merge_commit_sha`
`05393b5eac7fa7be68f5739eba92becb40619a50`.

Two GitHub Compare REST responses prove the immutable reviewed head is an
ancestor of the PR #71 merge and that the PR #71 merge is an ancestor of the
current deployment SHA. Each response must return the exact `base_commit.sha`
and `merge_base_commit.sha`, `behind_by=0`, an ancestral `status`, a complete
bounded commit page, and the exact final commit. The cumulative
PR71-merge-to-deployment `files` array must be untruncated and exactly these
seven existing, `modified`, non-renamed paths:

- `.github/workflows/fly-deploy-pr71-vault.yml`
- `docs/deployment/b3s_pr71_vault.md`
- `scripts/pr71_vault_database_target.py`
- `tests/test_configure_b3s_runtime_role.py`
- `tests/test_deploy_provenance.py`
- `tests/test_pr71_vault_deploy_target.py`
- `tests/test_pr71_vault_migration_target.py`

Any runtime, database, CI-workflow, config, or other source change after the PR
#71 merge is outside the hotfix allowlist and fails closed.

The trusted CI identity is pinned, not inherited from the old PR base:
workflow ID `306885838`, path `.github/workflows/ci.yml`, and Git blob
`c1082f6b5c53a364b8d38e43936b93722c0183d1`. The workflow must still be active,
and the Contents REST response at the reviewed PR head, PR #71 merge, and
current deployment SHA must return `type=file`, the exact path, and that exact
blob SHA. This deliberately accepts PR #71's sole reviewed CI edit,
`B3S_TEST_ALLOW_SCHEMA_DROP=1`, while rejecting any later CI drift.

The attestation also fetches and validates the exact successful PR CI run
`31496647341` and the exact successful PR #71 `push`/`main` CI run
`31506929921`. Finally it queries runs through the pinned workflow ID with
`event=push`, `branch=main`, and the exact current deployment `head_sha`, without filtering away
non-successful runs. Exactly one run may exist. Every accepted
run must return exact REST fields: `id`, `workflow_id`, `path`, `event`,
`head_branch`, `head_sha`, `status=completed`, `conclusion=success`,
`run_attempt=1`, plus repository ID `1288696741` and name `GsusFC/B3S` in
both `repository` and `head_repository`.
GitHub push runs do not need a PR linkage and their `pull_requests` array may be empty,
so it is intentionally not an acceptance condition. Missing, pending,
failed, foreign, stale, wrong-event, workflow-modified, truncated, or ambiguous
objects fail closed.

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
BRAND3_VAULT_OPERATIONAL_PIPELINE_ENABLED=true
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

## Rollback and NO-GO

Deployment is **NO-GO** unless the fixed PR #71 reviewed head and merge attest,
the requested SHA equals both dispatched and live current `main`, the only
post-PR71 changes are the exact three-file hotfix, all three CI blobs and all
required CI runs attest, the separate deployment GO and secret provisioning are
complete, the external migration target assertion passes before DDL, head `023`
verifies exactly, the idempotent runtime-role contract passes, and C7 remains
denied.
`b3s` and `b3s-vault` must remain on their SELECT-only release verifiers and may
not be used as fallback migration targets.

No older application image is assumed compatible with schema head `023`, and
this runbook pre-authorizes no image rollback. Engage the access/C7 holds first,
stop new scans, and drain the single active scan. Prefer a reviewed forward fix;
any coordinated image-plus-Neon-restore plan requires separate authorization and
proof against a preserved restore anchor. A release verifier mismatch is a hard
stop, not permission to run an older migrator. Database rollback is never an
in-place down migration. Preserve both database states and reconcile deltas if
any write occurred after a Neon rollback anchor. The encrypted Fly volume and
Neon branch must be retained together for the declared incident window.
