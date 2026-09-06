# One-time B3S production migration

`b3s-production-migration.yml` is the sole prepared path for an explicitly
authorized production history migration. It is deliberately separate from the
application deploy workflow: it does not install Fly credentials, does not run
`fly deploy`, and does not change `fly-deploy.yml`.

## Required protected environment

Before a separately authorized run, create the protected GitHub environment
`production-migration` with its required reviewers and deployment-branch policy
limited to `main`. Configure these values only on that environment:

- temporary environment secret `B3S_MIGRATION_DATABASE_URL`;
- temporary environment secret `B3S_PRODUCTION_TLS_ROOT_CERT`;
- `B3S_PRODUCTION_DATABASE`;
- `B3S_PRODUCTION_HOST`;
- `B3S_PRODUCTION_NEON_PROJECT_ID`;
- `B3S_PRODUCTION_NEON_BRANCH_ID`; and
- `B3S_PRODUCTION_MIGRATION_ROLE`.

The target module fails closed when any identity value is absent, when the DSN
does not exactly select the configured database, host, and migration role, or
when it lacks `sslmode=verify-full` and `channel_binding=require`. It removes
ambient libpq settings while it connects, then checks the actual TLS session,
database, `current_user`, Neon project, and Neon branch on the same connection
before the migration advisory lock or DDL.

The controller writes `B3S_PRODUCTION_TLS_ROOT_CERT` to a fresh mode-`0600`
temporary file and attaches that exact path as the DSN `sslrootcert` parameter.
It never uses `PGSSLROOTCERT`, system trust-store discovery, or a persistent
certificate file; the controller attempts to remove the temporary path in its
cleanup path before returning either a receipt or a sanitized failure.

Never enter a DSN in a command, workflow input, issue, pull request, or local
shell history. The production profile rejects `--database-url`; the controller
accepts no arguments at all.

## Controlled execution boundary

This document prepares no live action. A migration, environment-secret
provisioning, deployment, retry, or postflight investigation is not authorized
by this source change.

When separately authorized, dispatch the workflow only from `refs/heads/main`
and supply the exact 40-character SHA currently at `main`. Before checkout,
the workflow compares that input to the live GitHub `main` ref. It then checks
out exactly that SHA and verifies the clean checkout before exposing the
temporary database secret.

The controller invokes the existing migration script with the
`b3s-production` target profile, immediately performs target-attested
`verify_head` postflight, and emits only a bounded receipt containing status,
migration head, manifest count, and applied-count. It never prints the DSN,
identity values, raw database errors, or filesystem paths.

It installs only `requirements-b3s-production-migration.txt`: the reviewed,
hash-locked, binary-only migration dependency set. It does not resolve the
unbounded application dependency ranges in `pyproject.toml` during this
operation.

Remove the temporary environment secret after the authorized run and preserve
the sanitized workflow receipt. Any follow-up migration or application deploy
requires its own approval.
