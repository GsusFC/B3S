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
head-029 verifier, and `verify_pr71_vault_target.py`. The final verifier checks
the Fly app, base URL, authenticated TLS session, exact host/database/runtime
role/project/branch identity, the operational-pipeline capability, and access
gate. A mismatch aborts before an app Machine is updated. The
release command never applies DDL with the runtime `B3S_DATABASE_URL`.

## Access boundary

The isolated app sets `B3S_GOOGLE_OIDC_ENABLED=true`. Google OpenID Connect protects every browser, legacy, diagnostic, report, brand, artifact, and provider-check route. Access is authorized only for these exact verified Google Workspace identities:

- `jesus@wearefloc.com`
- `sergio@wearefloc.com`
- `javi@wearefloc.com`
- `victor@wearefloc.com`

The server validates Google signature, issuer, audience, expiry, nonce, PKCE, `email_verified=true`, exact `hd=wearefloc.com`, and exact email membership. The `hd` authorization-request parameter is only an account-picker hint. OAuth provider tokens are never persisted. Site sessions are signed, Secure, HttpOnly, SameSite=Lax, fixed to eight hours, and re-check the allowlist on every request. Authentication flow state expires after ten minutes.

The dedicated `/vault/review/*` surface remains a deliberate exception and continues to use only the distinct evidence-reviewer credential and its own signed session/CSRF boundary. Only exact `/health` and `/api/v1/*` otherwise bypass site OIDC; versioned API routes retain existing bearer scopes. Unsafe browser routes retain exact same-origin protection. Uvicorn access logging is disabled for this PR71 process so callback query codes and state do not enter application access logs. This does not prove that Fly edge telemetry omits query strings; verify and document the platform retention/redaction policy before deployment, and never expose edge logs containing OAuth callback queries.

The Google OAuth Web client must use this exact authorized redirect URI:

```text
https://b3s-pr71-vault.fly.dev/auth/google/callback
```

Store the OAuth client secret, session-signing secret, scanner bearer, and reviewer bearer in the approved secret manager. All four capabilities must be distinct. Never place credentials in files, URLs, screenshots, PR comments, or command history.


### OIDC transition without a public window

Create the company-controlled Google OAuth Web client before deployment and keep
the current Basic-gated Machine unchanged. Stage `B3S_GOOGLE_OIDC_CLIENT_ID`,
`B3S_GOOGLE_OIDC_CLIENT_SECRET`, and `B3S_GOOGLE_OIDC_SESSION_SECRET` without
restarting the old image. Only the newly reauthorized OIDC image reads them.
Its release verifier and startup both fail closed if any OIDC value, the exact
four-user allowlist, base URL, or secret-separation contract is wrong. Deploy the
new source and Fly config atomically; the old Machine remains Basic-gated until
replacement, while the new Machine either serves OIDC or fails startup. Verify
an allowed login, a disallowed login, exact SHA, API Bearer behavior, and the
separate reviewer surface before removing the now-unused Basic password secret.
The app has an attached volume, so this is a rolling single-Machine replacement,
not a canary or blue/green rollout.

## PostgreSQL identities

`B3S_DATABASE_URL` is a non-owner runtime login. It may perform the explicit
application DML contract but cannot create/alter/drop schema objects and cannot
read the migration-019 raw provenance relations. At first access, runtime code
opens a repeatable-read, read-only transaction and requires the exact packaged
migration versions, filenames, and checksums. The isolated release verifier
also requires `current_user = b3s_pr71_app_runtime`; an owner or migration
credential on the same branch is rejected before the Machine update.

Migrations 025–028 add the non-authoritative SV9 shadow assessment ledger,
structural hardening, the identity-only append writer, and one sanitized scalar-only
diagnostic view; the packaged migration head is 029. The raw ledger remains denied
to the web runtime. Only the exact PR71 runtime login receives `SELECT` on the view
when the separately controlled runtime-role configurator runs. The writer is append-only, and the single-item administrative runner is
dry-run by default (`--append` is the only persistent runner mode). Its expected
parent is the immutable packet parent. Under the adoption lock, a packet whose
parent is still current remains admissible by the original CAS contract; after
current advances, only the exact packet adopted by the latest event that directly
produced current remains admissible. At that post-adoption boundary, a sibling
sharing parent/source and any earlier producer fail closed. This does not alter
public scoring, ranking, API/UI, or cutover. Migration 024 preserves the strict
replay behavior from migration 023 and additionally projects exact active
accepted evidence relations into fresh planning. Migration 023 keeps the worker
replay reader strict after a report upgrades mutable `scan_runs` presentation
fields: it reconstructs the frozen raw scan projection from the immutable request
and plan, while retaining current source-run/status/error/requested/started and
non-lifecycle metadata so contamination still fails closed.

`run_evidence_vault_operational_sv9_shadow_work_items.py` is a one-shot external
administrative control-plane command, not an in-app worker. The separately reviewed
hourly scheduler invokes it through GitHub Actions. It discovers only an unassessed latest adoption whose exact packet
directly produced current memory; discovery returns only domain, packet
fingerprint, and immutable parent. The append method repeats all checks under the
promotion lock, so a discovery/adoption race fails closed. The command reads only
`B3S_PR71_SV9_SHADOW_WRITER_DATABASE_URL`, attests every connection to the exact
PR71 writer login/project/branch/TLS target, defaults to dry-run, and permits only
one item per explicit `--append` invocation. Its atomic report contains bounded
identity/status fields, never DSNs, packet payloads, evidence, tiles, score,
assessment UUID, or requirements. The separate
`pr71-sv9-shadow-automation.yml` control-plane workflow runs only from `main`,
processes at most one item on the first attempt of its hourly schedule; manual
dispatch and scheduled re-runs are dry-run only. It holds the writer URL only in the dedicated
`pr71-sv9-shadow-automation` GitHub environment. It has `contents: read` only and
contains no Fly, migration, OIDC, scanner, API, or runtime capability. Remove the
environment secret or disable that workflow to pause processing; rotate the writer
password to revoke a disclosed credential. Ledger history is never deleted during
pause or rollback.

The isolated L2 logins are fixed and distinct:

- `b3s_pr71_app_runtime` — FastAPI/report runtime login
- `b3s_pr71_scanner_ingest` — worker-only execute-only raw append, exact replay, and bounded planning-context login;
  the worker preflight binds the session to this exact role
- `b3s_pr71_c7_runtime_read` — external private provenance-diagnostic login
- `b3s_pr71_c7_governance` — external governance/adoption capability
- `b3s_pr71_sv9_shadow_writer` — external identity-only SV9 shadow append login;
  never install its URL in the Fly app

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
TLS state and is not part of this target attestation.
The approved secret manager must provision `B3S_MIGRATION_DATABASE_URL` only as a temporary secret
in the protected `pr71-vault` GitHub environment after controller merge, CI,
workflow-blob, zero-secret, and separate deployment-GO checks succeed. Never
paste, assign, or export the DSN in a local command or shell history.

Do not invoke the migrator, runtime-role configurator, `fly config validate`, or
`fly deploy` from a local checkout. The only authorized path is the post-merge
GitHub workflow dispatched from live `main`; its controller performs attestation
before checkout and secret use, then runs those packaged operations against the
attested current `main` payload, which is a hotfix descendant of PR #102.

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
target. Its current reviewed application baseline is PR #102 at head
`b476fff80cea9fb1f474bce24ebe511c06f30f7b` and merge commit
`0ac13c6e95ce0f413094838f2448578d25e90b2c`. Neither merging the reviewed
PR, the presence of `workflow_dispatch`, the confirmation input, nor a
successful attestation authorizes a merge or deployment. Merge approval and
the deployment GO remain separate operator decisions. The authorized scope is
the exact reviewed semantic-v3 application image on the already isolated
head-`029` database, with `B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED=false`.
It does not affect `b3s`, production authority, production data, or diagnostics.
The existing isolated operational pipeline, worker, verified-raw shadow, and
scheduler retain their independently authorized state; this reauthorization
changes none of their flags or credentials.

Dispatch only from exact `refs/heads/main` through the GitHub environment
`pr71-vault`. The `deployment_sha` input must equal the live `main` SHA; that
is the exact application payload checked out, built, and verified by `/health`.
The immutable dispatch SHA must separately equal the live REST `main` ref.
The environment has a custom deployment branch policy configured to allow only `main`.

The `pr71-vault` environment currently contains zero deployment secrets.
`B3S_MIGRATION_DATABASE_URL` and `FLY_API_TOKEN` were provisioned only for the
previous authorized deployment and then removed. Deployment run `31851012413`
completed every attestation, migration/ACL, deploy, and exact-commit liveness
step successfully at `8e6bc79da525e854e1a96fb5cad9f3837445f125`.
Both temporary deployment secrets were then removed.
Any future deployment requires a new exact-SHA GO and fresh secret provisioning.
The reviewed workflow source is trusted in this control-plane model.
The self-blob comparison is defense in depth against a stale or mismatched
dispatch; it does not
make mutable repository source an immutable root of trust or defend against an
actor able to merge a malicious workflow change. Before provisioning either
temporary secret, the operator must independently fetch the controller workflow
blob at live `main`, require it to equal both the reviewed expected blob and the
protected environment baseline, and confirm that the environment still contains zero
secrets. Any mismatch is a hard NO-GO. A compromised-maintainer threat model
would require an immutable external deployment controller and is not claimed here.

Before checkout, dependency installation, or deployment-secret use, pre-check
code embedded in the reviewed repository workflow fetches PR #102 and requires
`state=closed`, `merged=true`, and `draft=false`, base `main`, head
`fix/vault-semantic-coverage`, repository ID `1288696741` and
name `GsusFC/B3S`, exact reviewed head, and exact `merge_commit_sha`. It proves
reviewed-head-to-target and target-to-current-controller ancestry from complete
GitHub Compare responses. PR #102 remains the reviewed ancestry trust anchor.
The deployed tree is current `main`. For the attested hotfix controller, the
latter comparison must contain these exact existing, modified, non-renamed paths
recorded in the controller workflow `HOTFIX_FILES` set, including
`src/history/migrations/029_evidence_vault_semantic_analysis_claims.sql`.

Under that trusted-source model, any path after PR #102 that is outside the
attestation allowlist fails closed. This controller records the reviewed,
merged, green semantic-v3 baseline plus the attested 029 hotfix set.
Deployment remains a separate operator decision and requires temporary
migration and Fly capabilities.

The target tree cumulatively contains the already merged PRs #89, #91, #93,
#95, and #97; this controller does not reinterpret or selectively omit them.
The reauthorization trust anchor is PR #102: head
`b476fff80cea9fb1f474bce24ebe511c06f30f7b`, merge
`0ac13c6e95ce0f413094838f2448578d25e90b2c`, and CI runs `32006974926` and
`32007250803`.

The trusted CI identity is pinned to workflow ID `306885838`, path
`.github/workflows/ci.yml`, and blob
`c1082f6b5c53a364b8d38e43936b93722c0183d1`. The workflow must remain active;
the reviewed head, payload target, and current controller Contents responses
must carry that exact blob. Exact successful CI runs `32006974926` (reviewed PR
#102 head) and `32007250803` (PR #102 merge on `main`) are pinned. The current exact-main
controller push run is fetched without filtering away non-successful runs;
exactly one completed, successful, first-attempt run with the expected repository and SHA is accepted.
The `pull_requests` array may be empty. Missing, pending, failed, foreign, stale,
wrong-event, workflow-modified, truncated, or ambiguous objects fail closed.

## Required app secrets

L1 operation requires only app-scoped credentials:

- `B3S_DATABASE_URL` — isolated least-privilege runtime DSN
- `B3S_GOOGLE_OIDC_CLIENT_ID` — Google OAuth Web client identifier
- `B3S_GOOGLE_OIDC_CLIENT_SECRET` — Google OAuth client secret
- `B3S_GOOGLE_OIDC_SESSION_SECRET` — independent random site-session signing secret
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


## SV9 shadow diagnostic read (default off)

`B3S_VAULT_SV9_SHADOW_DIAGNOSTICS_ENABLED` is pinned to `false`. Even if set to
`true`, the gate also requires `BRAND3_ENVIRONMENT=vault`. Migration 028 exposes
only `evidence_vault_operational_sv9_shadow_diagnostics_v1`: scalar score
observations, immutable hashes, five verification-state counts, timestamps, and
the persisted false authority/effect flags. It omits the 80-tile vector, raw
assessment/verification JSON, evidence, packet/source IDs, reviewer material, and
assessment UUIDs. The raw table receives no runtime `SELECT` grant.

The hidden `/api/v1/brands/{domain}/vault-sv9-shadow-diagnostics` route requires
the dedicated evidence-reviewer bearer, not the scanner bearer. The separate
`/vault/diagnostics/sv9/{domain}` page additionally requires an actual Google site
session and is not linked from product/report/ranking pages. Both are bounded to
20 rows, use no-store caching, and remain diagnostic-only: they cannot select a
canonical score, change public scoring or ranking, write the ledger, or grant
runtime authority. Enabling the flag, applying migration 028, reconfiguring the
runtime ACL, and deploying an image are separate authorization boundaries.

## Post-deployment semantic-v3 validation

The deployment workflow proves exact image identity and liveness; it does not by
itself prove the forward-only report selector. Before declaring the rollout
validated, use an already authorized isolated scanner client outside the
`pr71-vault` deployment environment to generate exactly one new report for a
preapproved Vault domain. Do not add scanner or reviewer credentials to the
deployment environment.

The new report must be `completed` and expose all of these exact bindings:

- `metadata.pipeline_schema_version == b3s-vault-semantic-report-v2`
- `metadata.pipeline_commit_sha` must equal the dispatched `deployment_sha`
- `metadata.rubric_version == baldosas-v3-1`
- `metadata.evaluator_model == evidence-vault-semantic-scoring-v3`
- limitation `score_projected_from_evidence_vault_semantic_scoring_v3`

Capture an exact digest of one historical Vault report before and after the smoke
and require byte identity. Separately require the new semantic-v3 report to keep
`raw.vault.legacy_operational_v2.score_evaluation` and
`raw.vault.legacy_operational_v2.score_authority_witness` as its legacy witness.
Confirm diagnostics remain `false` and no report, credential, or evidence appears
in `b3s` or `b3s-vault`. Contradiction, tampering, a missing source, invalid
bindings, selector fallback, or any metadata mismatch is a
NO-GO: stop, preserve receipts, remove the two temporary deployment secrets,
and do not backfill, promote evidence, or roll back automatically.

## Rollback and NO-GO

This exact three-file reauthorization replaces the former deployment NO-GO only
for the reviewed PR #102 semantic-v3 baseline. It becomes effective after it is
merged, its exact current-main CI succeeds, the independently reviewed
deployment-workflow blob is pinned in the protected environment, that environment
is reconfirmed to contain zero deployment secrets,
and the operator records an exact-SHA deployment GO. The allowlist must not be weakened. The authorized
workflow may idempotently verify/apply the packaged head 029, reassert the exact
runtime ACL, and deploy the exact current `main` image with diagnostics still
`false`. The controller is checked out only as part of attested current main.
The external migration-target assertion must pass before any DDL and head `029` must
verify exactly. No new flag activation or scheduler-state change is authorized.
`b3s` and `b3s-vault` must remain on their SELECT-only release verifiers and may
not be used as fallback migration targets.

No older application image is assumed compatible with schema head `029`, and
this runbook pre-authorizes no image rollback. Engage the access and scan holds first,
stop new scans, and drain the single active scan. Prefer a reviewed forward fix;
any coordinated image-plus-Neon-restore plan requires separate authorization and
proof against a preserved restore anchor. A release verifier mismatch is a hard
stop, not permission to run an older migrator. Database rollback is never an
in-place down migration. Preserve both database states and reconcile deltas if
any write occurred after a Neon rollback anchor. The encrypted Fly volume and
Neon branch must be retained together for the declared incident window.
